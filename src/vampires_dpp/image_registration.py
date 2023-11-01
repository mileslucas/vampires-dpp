import warnings

import numpy as np
from astropy.io import fits
from astropy.modeling import fitting, models
from numpy.typing import ArrayLike
from photutils import centroids
from skimage.measure import centroid
from skimage.registration import phase_cross_correlation

from .indexing import frame_center


def offset_dft(frame, inds, psf, *, upsample_factor):
    cutout = frame[inds]
    dft_offset = phase_cross_correlation(
        psf, cutout, return_error=False, upsample_factor=upsample_factor
    )
    ctr = np.array(frame_center(psf)) - dft_offset
    # offset based on indices
    ctr[-2] += inds[-2].start
    ctr[-1] += inds[-1].start
    return ctr


def offset_centroids(frame, frame_err, inds):
    """NaN-friendly centroids"""
    # wy, wx = np.ogrid[inds[-2], inds[-1]]
    cutout = frame[inds]
    if frame_err is not None:
        cutout_err = frame_err[inds]
    else:
        cutout_err = None

    peak_yx = np.unravel_index(np.nanargmax(cutout), cutout.shape)
    com_xy = centroids.centroid_com(cutout)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        gauss_xy = centroids.centroid_2dg(cutout, error=cutout_err)
        quad_xy = centroids.centroid_quadratic(
            cutout, xpeak=com_xy[0], ypeak=com_xy[1], fit_boxsize=cutout.shape
        )

    # offset based on indices
    offx = inds[-1].start
    offy = inds[-2].start
    ctrs = {
        "peak": np.array((peak_yx[0] + offy, peak_yx[1] + offx)),
        "com": np.array((com_xy[1] + offy, com_xy[0] + offx)),
        "gauss": np.array((gauss_xy[1] + offy, gauss_xy[0] + offx)),
        "quad": np.array((quad_xy[1] + offy, quad_xy[0] + offx)),
    }
    return ctrs
