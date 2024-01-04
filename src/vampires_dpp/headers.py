import numpy as np
from astropy.io import fits
from astropy.time import Time

from vampires_dpp.constants import CMOSVAMPIRES, EMCCDVAMPIRES, SUBARU_LOC, InstrumentInfo
from vampires_dpp.util import wrap_angle


def parallactic_angle(time, coord):
    pa = parallactic_angle_hadec(
        time.sidereal_time("apparent").hourangle - coord.ra.hourangle, coord.dec.deg
    )
    return wrap_angle(pa)


def parallactic_angle_hadec(ha, dec, lat=SUBARU_LOC.lat.deg):
    r"""Calculate parallactic angle using the hour-angle and declination directly

    .. math::

        \theta_\mathrm{PA} = \arctan2{\frac{\sin\theta_\mathrm{HA}}{\tan\theta_\mathrm{lat}\cos\delta - \sin\delta \cos\theta_\mathrm{HA}}}

    Parameters
    ----------
    ha : float
        hour-angle, in hour angles
    dec : float
        declination in degrees
    lat : float, optional
        latitude of observation in degrees, by default 19.823806

    Returns
    -------
    float
        parallactic angle, in degrees East of North
    """
    _ha = ha * np.pi / 12  # hour angle to radian
    _dec = np.deg2rad(dec)
    _lat = np.deg2rad(lat)
    sin_ha, cos_ha = np.sin(_ha), np.cos(_ha)
    sin_dec, cos_dec = np.sin(_dec), np.cos(_dec)
    pa = np.arctan2(sin_ha, np.tan(_lat) * cos_dec - sin_dec * cos_ha)
    return np.rad2deg(pa)


def parallactic_angle_altaz(alt, az, lat=SUBARU_LOC.lat.deg):
    r"""Calculate parallactic angle using the altitude/elevation and azimuth directly

    Parameters
    ----------
    alt : float
        altitude or elevation, in degrees
    az : float
        azimuth, in degrees CCW from North
    lat : float, optional
        latitude of observation in degrees, by default 19.823806

    Returns
    -------
    float
        parallactic angle, in degrees East of North
    """
    ## Astronomical Algorithms, Jean Meeus
    # get angles, rotate az to S
    _az = np.deg2rad(az) - np.pi
    _alt = np.deg2rad(alt)
    _lat = np.deg2rad(lat)
    # calculate values ahead of time
    sin_az, cos_az = np.sin(_az), np.cos(_az)
    sin_alt, cos_alt = np.sin(_alt), np.cos(_alt)
    sin_lat, cos_lat = np.sin(_lat), np.cos(_lat)
    # get declination
    dec = np.arcsin(sin_alt * sin_lat - cos_alt * cos_lat * cos_az)
    # get hour angle
    ha = np.arctan2(sin_az, cos_az * sin_lat + np.tan(_alt) * cos_lat)
    # get parallactic angle
    pa = np.arctan2(np.sin(ha), np.tan(_lat) * np.cos(dec) - np.sin(dec) * np.cos(ha))
    return np.rad2deg(pa)


def fix_header(header):
    # check if the millisecond delimiter is a colon
    for key in ("UT-STR", "UT-END", "HST-STR", "HST-END"):
        if key in header and header[key].count(":") == 3:
            tokens = header[key].rpartition(":")
            header[key] = f"{tokens[0]}.{tokens[2]}", header.comments[key]
    # fix UT/HST/MJD being time of file creation instead of typical time
    for key in ("UT", "HST"):
        if f"{key}-STR" in header and f"{key}-END" in header:
            header[key] = fix_typical_time_iso(header, key), header.comments[key]
    if "MJD-STR" in header and "MJD-END" in header:
        header["MJD"] = fix_typical_time_mjd(header), header.comments["MJD"]
    # add RET-ANG1 and similar headers to be more consistent
    if "DETECTOR" not in header:
        header["DETECTOR"] = (f"VCAM{header['U_CAMERA']:.0f} - Ultra 897", "Name of the detector")
    if "OBS-MOD" not in header:
        header["OBS-MOD"] = "IPOL", "Observation mode"
    if "U_EMGAIN" in header:
        header["DETGAIN"] = header["U_EMGAIN"], "Detector multiplication factor"
    if "U_HWPANG" in header:
        header["RET-ANG1"] = (header["U_HWPANG"], "[deg] Position angle of first retarder plate")
        header["RETPLAT1"] = "HWP(NIR)", "Identifier of first retarder plate"
    if "U_FLCSTT" in header:
        header["U_FLC"] = "A" if header["U_FLCSTT"] == 1 else "B", "VAMPIRES FLC State"
    if "EXPTIME" not in header:
        header["EXPTIME"] = (header["U_AQTINT"] / 1e6, "[s] Total integration time of the frame")
    if "FILTER01" not in header:
        header["FILTER01"] = header["U_FILTER"], "Primary filter name"
        header["FILTER02"] = "Unknown", "Secondary filter name"
    if "PRD-MIN1" not in header:
        full_size = 512
        crop_size = header["NAXIS2"], header["NAXIS1"]
        start_idx = np.round((full_size - np.array(crop_size)) / 2)
        header["PRD-MIN1"] = start_idx[-1], "[pixel] Origin in X of the cropped window"
        header["PRD-MIN2"] = start_idx[-2], "[pixel] Origin in Y of the cropped window"
        header["PRD-RNG1"] = crop_size[-1], "[pixel] Range in X of the cropped window"
        header["PRD-RNG2"] = crop_size[-2], "[pixel] Range in Y of the cropped window"
    if "TINT" not in header:
        header["TINT"] = (
            header["EXPTIME"] * header.get("NAXIS3", 1),
            "[s] Total integration time of file",
        )
    if "NFRAMES" not in header:
        header["NFRAMES"] = header.get("NAXIS3", 1), "Number of frames in original file"

    # add in detector charracteristics
    inst = get_instrument_from(header)
    header["BIAS"] = inst.bias, "[adu] Bias offset"
    header["GAIN"] = inst.gain, "[e-/adu] Detector gain"
    header["DC"] = inst.dark_current, "[e-/s/pix] Detector dark current"
    header["ENF"] = inst.excess_noise_factor, "Detector excess noise factor"
    header["EFFGAIN"] = inst.effgain, "[e-/adu] Detector effective gain"
    header["RN"] = inst.readnoise, "[e-] RMS read noise"
    header["PXSCALE"] = inst.pixel_scale, "[mas/pix] Pixel scale"
    header["PAOFFSET"] = inst.pa_offset, "[deg] Parallactic angle offset"
    header["INST-PA"] = inst.pupil_offset, "[deg] Instrument angle offset"
    header["FULLWELL"] = inst.fullwell, "[e-] Full well of detector register"

    return header


def fix_typical_time_iso(hdr, key):
    """Return the middle point of the exposure for ISO-based timestamps

    Parameters
    ----------
    hdr : FITSHeader
    key : str
        key to fix, e.g. "UT"

    Returns
    -------
    str
        The ISO timestamp (hh:mm:ss.sss) for the middle point of the exposure
    """
    date = hdr["DATE-OBS"]
    # get start time
    key_str = f"{key}-STR"
    t_str = Time(f"{date}T{hdr[key_str]}", format="fits", scale="ut1")
    # get end time
    key_end = f"{key}-END"
    t_end = Time(f"{date}T{hdr[key_end]}", format="fits", scale="ut1")
    # get typical time as midpoint of two times
    dt = (t_end - t_str) / 2
    t_typ = t_str + dt
    # split on the space to remove date from timestamp
    return t_typ.iso.split()[-1]


def fix_typical_time_mjd(hdr):
    """Return the middle point of the exposure for MJD timestamps

    Parameters
    ----------
    hdr : FITSHeader

    Returns
    -------
    str
        The MJD timestamp for the middle point of the exposure
    """
    # repeat again for MJD with different format
    t_str = Time(hdr["MJD-STR"], format="mjd", scale="ut1")
    t_end = Time(hdr["MJD-END"], format="mjd", scale="ut1")
    # get typical time as midpoint of two times
    dt = (t_end - t_str) / 2
    t_typ = t_str + dt
    # split on the space to remove date from timestamp
    return t_typ.mjd


def get_instrument_from(header: fits.Header) -> InstrumentInfo:
    """Get the instrument info from a FITS header"""
    cam_num = int(header["U_CAMERA"])
    if "U_EMGAIN" in header:
        inst = EMCCDVAMPIRES(cam_num=cam_num, emgain=header["U_EMGAIN"])
    else:
        inst = CMOSVAMPIRES(cam_num=cam_num, readmode=header["U_DETMOD"].lower())

    return inst


def sort_header(header: fits.Header) -> fits.Header:
    """Sort all non-structural FITS header keys"""
    output_header = fits.Header()
    for key in sorted(header):
        # skip structural keys
        if key in ("SIMPLE", "BITPIX", "EXTEND", "COMMENT", "HISTORY") or key.startswith("NAXIS"):
            continue

        output_header[key] = header[key], header.comments[key]
    return output_header
