from pathlib import Path
from typing import Optional, Sequence, Tuple

import numpy as np
import tqdm.auto as tqdm
from astropy.io import fits
from astropy.wcs import WCS
from astropy.wcs.utils import add_stokes_axis_to_wcs
from numpy.typing import ArrayLike, NDArray
from photutils import CircularAnnulus, CircularAperture, aperture_photometry
from scipy.optimize import minimize_scalar

from .constants import PUPIL_OFFSET
from .headers import observation_table
from .image_processing import combine_frames_headers, derotate_cube
from .image_registration import offset_centroid
from .indexing import frame_angles, frame_center, frame_radii, window_slices
from .mueller_matrices import mirror, mueller_matrix_model, rotator
from .util import average_angle
from .wcs import apply_wcs

HWP_POS_STOKES = {0: "Q", 45: "-Q", 22.5: "U", 67.5: "-U"}


def measure_instpol(I: ArrayLike, X: ArrayLike, r=5, center=None, expected=0):
    """
    Use aperture photometry to estimate the instrument polarization.

    Parameters
    ----------
    stokes_cube : ArrayLike
        Input Stokes cube (4, y, x)
    r : float, optional
        Radius of circular aperture in pixels, by default 5
    center : Tuple, optional
        Center of circular aperture (y, x). If None, will use the frame center. By default None
    expected : float, optional
        The expected fractional polarization, by default 0

    Returns
    -------
    float
        The instrumental polarization coefficient
    """
    if center is None:
        center = frame_center(I)

    x = X / I

    rs = frame_radii(x)

    weights = np.ones_like(x)
    # only keep values inside aperture
    weights[rs > r] = 0

    pX = np.nansum(x * weights) / np.nansum(weights)
    return pX - expected


def measure_instpol_satellite_spots(I: ArrayLike, X: ArrayLike, r=5, expected=0, **kwargs):
    """
    Use aperture photometry on satellite spots to estimate the instrument polarization.

    Parameters
    ----------
    stokes_cube : ArrayLike
        Input Stokes cube (4, y, x)
    r : float, optional
        Radius of circular aperture in pixels, by default 5
    center : Tuple, optional
        Center of satellite spots (y, x). If None, will use the frame center. By default None
    radius : float
        Radius of satellite spots in pixels
    expected : float, optional
        The expected fractional polarization, by default 0

    Returns
    -------
    float
        The instrumental polarization coefficient
    """
    x = X / I

    slices = window_slices(x, **kwargs)
    # refine satellite spot apertures onto centroids
    aps_centers = [offset_centroid(I, sl) for sl in slices]

    # TODO may be biased by central halo?
    # measure IP from aperture photometry
    aps = CircularAperture(aps_centers, r)
    fluxes = aperture_photometry(x, aps)["aperture_sum"]
    cX = np.mean(fluxes) / aps.area

    return cX - expected


def instpol_correct(stokes_cube: ArrayLike, pQ=0, pU=0, pV=0):
    """
    Apply instrument polarization correction to stokes cube.

    Parameters
    ----------
    stokes_cube : ArrayLike
        (4, ...) array of stokes values
    pQ : float, optional
        I -> Q contribution, by default 0
    pU : float, optional
        I -> U contribution, by default 0
    pV : float, optional
        I -> V contribution, by default 0

    Returns
    -------
    NDArray
        (4, ...) stokes cube with corrected parameters
    """
    return np.array(
        (
            stokes_cube[0],
            stokes_cube[1] - pQ * stokes_cube[0],
            stokes_cube[2] - pU * stokes_cube[0],
            stokes_cube[3] - pV * stokes_cube[0],
        )
    )


def background_subtracted_photometry(frame, aps, anns):
    ap_sums = aperture_photometry(frame, aps)["aperture_sum"]
    ann_sums = aperture_photometry(frame, anns)["aperture_sum"]
    return ap_sums - aps.area / anns.area * ann_sums


def radial_stokes(stokes_cube: ArrayLike, phi: Optional[float] = None, **kwargs) -> NDArray:
    r"""
    Calculate the radial Stokes parameters from the given Stokes cube (4, N, M)

    ..math::
        Q_\phi = -Q\cos(2\theta) - U\sin(2\theta) \\
        U_\phi = Q\sin(2\theta) - Q\cos(2\theta)


    Parameters
    ----------
    stokes_cube : ArrayLike
        Input Stokes cube, with dimensions (4, N, M)
    phi : float, optional
        Radial angle offset in radians. If None, will automatically optimize the angle with ``optimize_Uphi``, which minimizes the Uphi signal. By default None

    Returns
    -------
    NDArray, NDArray
        Returns the tuple (Qphi, Uphi)
    """
    thetas = frame_angles(stokes_cube)
    if phi is None:
        phi = optimize_Uphi(stokes_cube, thetas, **kwargs)

    cos2t = np.cos(2 * (thetas + phi))
    sin2t = np.sin(2 * (thetas + phi))
    Qphi = -stokes_cube[1] * cos2t - stokes_cube[2] * sin2t
    Uphi = stokes_cube[1] * sin2t - stokes_cube[2] * cos2t

    return Qphi, Uphi


def optimize_Uphi(stokes_cube: ArrayLike, thetas: ArrayLike, r=8) -> float:
    rs = frame_radii(stokes_cube)
    mask = rs <= r
    masked_stokes_cube = stokes_cube[..., mask]
    masked_thetas = thetas[..., mask]

    loss = lambda X: Uphi_loss(X, masked_stokes_cube, masked_thetas, r=r)
    res = minimize_scalar(loss, bounds=(-np.pi / 2, np.pi / 2), method="bounded")
    return res.x


def Uphi_loss(X: float, stokes_cube: ArrayLike, thetas: ArrayLike, r) -> float:
    cos2t = np.cos(2 * (thetas + X))
    sin2t = np.sin(2 * (thetas + X))
    Uphi = stokes_cube[1] * sin2t - stokes_cube[2] * cos2t
    l2norm = np.nansum(Uphi**2)
    return l2norm


def collapse_stokes_cube(stokes_cube, pa, header=None):
    stokes_out = np.empty_like(stokes_cube, shape=(stokes_cube.shape[0], *stokes_cube.shape[-2:]))
    for s in range(stokes_cube.shape[0]):
        derot = derotate_cube(stokes_cube[s], pa)
        stokes_out[s] = np.median(derot, axis=0)

    # now that cube is derotated we can apply WCS
    if header is not None:
        apply_wcs(header, pupil_offset=None)

    return stokes_out, header


def polarization_calibration_triplediff(filenames: Sequence[str], outname) -> NDArray:
    """
    Return a Stokes cube using the _bona fide_ triple differential method. This method will split the input data into sets of 16 frames- 2 for each camera, 2 for each FLC state, and 4 for each HWP angle.

    .. admonition:: Pupil-tracking mode
        :class: warning
        For each of these 16 image sets, it is important to consider the apparant sky rotation when in pupil-tracking mode (which is the default for most VAMPIRES observations). With this naive triple-differential subtraction, if there is significant sky motion, the output Stokes frame will be smeared.

        The parallactic angles for each set of 16 frames should be averaged (``average_angle``) and stored to construct the final derotation angle vector

    Parameters
    ----------
    filenames : Sequence[str]
        List of input filenames to construct Stokes frames from

    Raises
    ------
    ValueError:
        If the input filenames are not a clean multiple of 16. To ensure you have proper 16 frame sets, use ``flc_inds`` with a sorted observation table.

    Returns
    -------
    NDArray
        (4, t, y, x) Stokes cube from all 16 frame sets.
    """
    if len(filenames) % 8 != 0:
        raise ValueError(
            "Cannot do triple-differential calibration without exact sets of 8 frames for each HWP cycle"
        )
    # now do triple-differential calibration
    # only load 8 files at a time to avoid running out of memory on large datasets
    N_hwp_sets = len(filenames) // 8
    with fits.open(filenames.iloc[0]) as hdus:
        stokes_cube = np.zeros(shape=(4, N_hwp_sets, *hdus[0].shape[-2:]), dtype=hdus[0].data.dtype)
    iter = tqdm.trange(N_hwp_sets, desc="Triple-differential calibration")
    for i in iter:
        # prepare input frames
        ix = i * 8  # offset index
        summ_dict = {}
        diff_dict = {}
        matrix_dict = {}
        for file in filenames.iloc[ix : ix + 8]:
            stack, hdr = fits.getdata(file, header=True)
            key = hdr["U_HWPANG"], hdr["U_FLCSTT"]
            summ_dict[key] = stack[0]
            diff_dict[key] = stack[1]

            pa = np.deg2rad(hdr["D_IMRPAD"] + 180 - hdr["D_IMRPAP"])
            altitude = np.deg2rad(hdr["ALTITUDE"])
            hwp_theta = np.deg2rad(hdr["U_HWPANG"])
            imr_theta = np.deg2rad(hdr["D_IMRANG"])
            # qwp are oriented with 0 on vertical axis
            qwp1 = np.deg2rad(hdr["U_QWP1"]) + np.pi / 2
            qwp2 = np.deg2rad(hdr["U_QWP2"]) + np.pi / 2

            # get matrix for camera 1
            M1 = mueller_matrix_model(
                camera=1,
                filter=hdr["U_FILTER"],
                flc_state=hdr["U_FLCSTT"],
                qwp1=qwp1,
                qwp2=qwp2,
                imr_theta=imr_theta,
                hwp_theta=hwp_theta,
                pa=pa,
                altitude=altitude,
            )
            # get matrix for camera 2
            M2 = mueller_matrix_model(
                camera=2,
                filter=hdr["U_FILTER"],
                flc_state=hdr["U_FLCSTT"],
                qwp1=qwp1,
                qwp2=qwp2,
                imr_theta=imr_theta,
                hwp_theta=hwp_theta,
                pa=pa,
                altitude=altitude,
            )
            matrix_dict[key] = M1 - M2
        ## make difference images
        # double difference (FLC1 - FLC2)
        pQ = 0.5 * (diff_dict[(0, 1)] - diff_dict[(0, 2)])
        pIQ = 0.5 * (summ_dict[(0, 1)] + summ_dict[(0, 2)])
        M_pQ = 0.5 * (matrix_dict[(0, 1)] - matrix_dict[(0, 2)])

        mQ = 0.5 * (diff_dict[(45, 1)] - diff_dict[(45, 2)])
        mIQ = 0.5 * (summ_dict[(45, 1)] + summ_dict[(45, 2)])
        M_mQ = 0.5 * (matrix_dict[(45, 1)] - matrix_dict[(45, 2)])

        pU = 0.5 * (diff_dict[(22.5, 1)] - diff_dict[(22.5, 2)])
        pIU = 0.5 * (summ_dict[(22.5, 1)] + summ_dict[(22.5, 2)])
        M_pU = 0.5 * (matrix_dict[(22.5, 1)] - matrix_dict[(22.5, 2)])

        mU = 0.5 * (diff_dict[(67.5, 1)] - diff_dict[(67.5, 2)])
        mIU = 0.5 * (summ_dict[(67.5, 1)] + summ_dict[(67.5, 2)])
        M_mU = 0.5 * (matrix_dict[(67.5, 1)] - matrix_dict[(67.5, 2)])

        # triple difference (HWP1 - HWP2)
        Q = 0.5 * (pQ - mQ)
        IQ = 0.5 * (pIQ + mIQ)
        M_Q = 0.5 * (M_pQ - M_mQ)
        U = 0.5 * (pU - mU)
        IU = 0.5 * (pIU + mIU)
        M_U = 0.5 * (M_pU - M_mU)
        I = 0.5 * (IQ + IU)

        # IP corr
        Q -= M_Q[1, 0] * I
        U -= M_U[2, 0] * I

        # crosstalk corr
        QU_mat = np.asarray((Q.ravel(), U.ravel()))
        M_QU = np.asarray((M_Q[0, 1:3], M_U[0, 1:3]))
        QU_corr = np.linalg.lstsq(M_QU, QU_mat, rcond=None)[0]

        Q_corr = QU_corr[0].reshape(Q.shape)
        U_corr = QU_corr[1].reshape(U.shape)

        stokes_cube[:3, i] = I, Q, U  # Q_corr, U_corr

    headers = [fits.getheader(f) for f in filenames]
    stokes_hdr = combine_frames_headers(headers)

    return write_stokes_products(stokes_cube, stokes_hdr, outname=outname)


def triplediff_average_angles(filenames):
    if len(filenames) % 8 != 0:
        raise ValueError(
            "Cannot do triple-differential calibration without exact sets of 8 frames for each HWP cycle"
        )
    # make sure we get data in correct order using FITS headers
    tbl = observation_table(filenames).sort_values("DATE")

    N_hwp_sets = len(tbl) // 8
    pas = np.zeros(N_hwp_sets, dtype="f4")
    for i in range(pas.shape[0]):
        ix = i * 8
        pas[i] = average_angle(tbl["D_IMRPAD"].iloc[ix : ix + 8] + PUPIL_OFFSET)

    return pas


def pol_inds(flc_states: ArrayLike, n=4):
    """
    Find consistent runs of FLC and HWP states.

    A consistent FLC run will have either 2 or 4 files per HWP state, and will have exactly 4 HWP states per cycle. Sometimes when VAMPIRES is syncing with CHARIS a HWP state will get skipped, creating partial HWP cycles. This function will return the indices which create consistent HWP cycles from the given list of FLC states, which should already be sorted by time.

    Parameters
    ----------
    hwp_polstt : ArrayLike
        The HWP states to sort through
    n : int, optional
        The number of files per HWP state, either 2 or 4. By default 4

    Returns
    -------
    inds :
        The indices for which `flc_states` forms consistent HWP cycles
    """
    states = np.asarray(flc_states)
    N_cycle = n * 4
    ang_list = np.repeat([0, 45, 22.5, 67.5], n)
    inds = []
    idx = 0
    while idx <= len(flc_states) - N_cycle:
        if np.all(states[idx : idx + N_cycle] == ang_list):
            inds.extend(range(idx, idx + N_cycle))
            idx += N_cycle
        else:
            idx += 1

    return inds


def polarization_calibration_model(filename):
    header = fits.getheader(filename)
    pa = np.deg2rad(header["D_IMRPAD"] + 180 - header["D_IMRPAP"])
    altitude = np.deg2rad(header["ALTITUDE"])
    hwp_theta = np.deg2rad(header["U_HWPANG"])
    imr_theta = np.deg2rad(header["D_IMRANG"])
    # qwp are oriented with 0 on vertical axis
    qwp1 = np.deg2rad(header["U_QWP1"]) + np.pi / 2
    qwp2 = np.deg2rad(header["U_QWP2"]) + np.pi / 2

    M = mueller_matrix_model(
        camera=header["U_CAMERA"],
        filter=header["U_FILTER"],
        flc_state=header["U_FLCSTT"],
        qwp1=qwp1,
        qwp2=qwp2,
        imr_theta=imr_theta,
        hwp_theta=hwp_theta,
        pa=pa,
        altitude=altitude,
    )
    return M


def mueller_mats_file(filename, output=None, skip=False):
    if output is None:
        indir = Path(filename).parent
        output = indir / f"mueller_mats.fits"
    else:
        output = Path(output)

    if skip and output.is_file():
        return output

    mueller_mat = polarization_calibration_model(filename)

    hdu = fits.PrimaryHDU(mueller_mat)
    hdu.header["INPUT"] = filename.absolute(), "FITS diff frame"
    hdu.writeto(output, overwrite=True)

    return output


def mueller_matrix_calibration(mueller_matrices: ArrayLike, cube: ArrayLike) -> NDArray:
    stokes_cube = np.zeros((mueller_matrices.shape[-1], cube.shape[-2], cube.shape[-1]))
    # go pixel-by-pixel
    for i in range(cube.shape[-2]):
        for j in range(cube.shape[-1]):
            stokes_cube[:, i, j] = np.linalg.lstsq(mueller_matrices, cube[:, i, j], rcond=None)[0]

    return stokes_cube


def write_stokes_products(stokes_cube, header=None, outname=None, skip=False, phi=0):
    if outname is None:
        path = Path("stokes_cube.fits")
    else:
        path = Path(outname)

    if skip and path.is_file():
        return path

    pi = np.hypot(stokes_cube[2], stokes_cube[1])
    aolp = np.arctan2(stokes_cube[2], stokes_cube[1])
    Qphi, Uphi = radial_stokes(stokes_cube, phi=phi)

    if header is None:
        header = fits.Header()

    header["STOKES"] = "I,Q,U,Qphi,Uphi,LP_I,AoLP"
    if phi is not None:
        header["VPP_PHI"] = phi, "deg, angle of linear polarization offset"

    data = np.asarray((stokes_cube[0], stokes_cube[1], stokes_cube[2], Qphi, Uphi, pi, aolp))

    fits.writeto(path, data, header=header, overwrite=True)

    return path
