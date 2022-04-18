import numpy as np
import pytest

from vampires_dpp.image_processing import shift_frame, derotate_frame, frame_center


def test_shift_frame():
    array = np.asarray([[0, 0, 0], [0, 1, 0], [0, 0, 0]])

    shift_down = shift_frame(array, (-1, 0))
    assert np.allclose(shift_down, np.array([[0, 1, 0], [0, 0, 0], [0, 0, 0]]))

    shift_up = shift_frame(array, (1, 0))
    assert np.allclose(shift_up, np.array([[0, 0, 0], [0, 0, 0], [0, 1, 0]]))

    shift_left = shift_frame(array, (0, -1))
    assert np.allclose(shift_left, np.array([[0, 0, 0], [1, 0, 0], [0, 0, 0]]))

    shift_right = shift_frame(array, (0, 1))
    assert np.allclose(shift_right, np.array([[0, 0, 0], [0, 0, 1], [0, 0, 0]]))


def test_derotate_frame():
    array = np.asarray([[0, 0, 0], [1, 0, 0], [0, 0, 0]])

    cw_90 = derotate_frame(array, 90)
    assert np.allclose(cw_90, np.array([[0, 0, 0], [0, 0, 0], [0, 1, 0]]))

    ccw_90 = derotate_frame(array, -90)
    assert np.allclose(ccw_90, np.array([[0, 1, 0], [0, 0, 0], [0, 0, 0]]))


@pytest.mark.parametrize(
    "frame,center",
    [
        (np.empty((10, 10)), (4.5, 4.5)),
        (np.empty((11, 11)), (5, 5)),
        (np.empty((100, 11, 11)), (5, 5)),
        (np.empty((10, 100, 16, 11)), (7.5, 5)),
    ],
)
def test_frame_center(frame, center):
    fcenter = frame_center(frame)
    assert fcenter[0] == center[0]
    assert fcenter[1] == center[1]