# Pipeline

The VAMPIRES data processing pipeline uses a configuration file to automate the bulk reduction of VAMPIRES data. To run the pipeline, use the `vpp` script

```
usage: vpp [-h] [-v] config

positional arguments:
  config         path to configuration file

optional arguments:
  -h, --help     show this help message and exit
  -v, --verbose  Print (extremely) verbose logging
```

The pipeline will reduce the data in the following order
1. Calibration
2. Frame Selection
3. Image Registration
4. Coadding
5. Derotation

```{admonition} Troubleshooting
:class: tip 
If you run into problems, try running the pipeline in verbose mode (with the `-v` flag). This will output an overwhelming amount of text, so you may want to pipe this into a file

.. code::
    vpp config.toml -v
    vpp config.toml -v > vpp_output.txt
```


## Configuration

The configuration file uses the [TOML](https://toml.io) format. There are many options that have defaults, sometimes even sensible ones. In general, if an entire section is missing, the operation will be excluded. Note that sections for the configuration (e.g., `[calibration]`, `[frame_selection]`) can be in any order, although keeping them in the same order as the pipeline execution may be clearer.

### Version

```toml
version = "0.1.0" # vampires_dpp version
```

The first required option is the `version` which should be set to the same value as your `vampires_dpp` version, which you can get from

```
python -c 'import vampires_dpp as vpp; print(vpp.__version__)'
```

We use [semantic versioning](https://semver.org/), so there are certain guarantees about the backwards compatibility of the pipeline, which means you don't have to have the exact same version of `vampires_dpp` as in the configuration- merely a version that is compatible.

### Global Options

```toml
name = "example" 
```
A simple filename-safe name that is used for some automatic naming.

```toml
directory = ""
```
The absolute path of the top-level data directory

```toml
output_directory = "" # optional
```

absolute path to top-level output directory, if empty will use the root directory


```toml
frame_centers = [[128, 129], [130, 129]] # optional
```
A list of frame centers for cam1 and cam2 from visual inspection. This is useful in the case that the PSFs are not well-centered in the frame, which can make the windows used for frame selection or image registration poorly positioned.

The frame centers must be given as a list of (x, y) lists, otherwise they will default to the geometric frame centers

### Coronagraph Options

If you are reducing coronagraphic data, you will need to add the following section

```toml
[coronagraph] # optional
```

```toml
mask_size = 90 # mas
```

The coronagraph mask inner working angle (IWA), in mas. The IWAs for the masks are listed on the [VAMPIRES website](https://www.naoj.org/Projects/SCEXAO/scexaoWEB/030openuse.web/040vampires.web/100vampcoronagraph.web/indexm.html).

```toml
[coronagraph.satellite_spots] # optional
```

This section signifies that satellite spots were used and will change how certain reduction steps are done. For frame selection and image registration, the satellite spots will be used instead of the central PSF, which is obscured by the coronagraph.

```toml
radius = 15.9 # lam/D
```

The radius of the satellite spots in lambda/D. This is logged in the CHARIS data but otherwise must be communicated by SCExAO support astronomers.

```toml
angle = -4 # deg, optional
```

The angle, in degrees, of the closest satellite spot to the positive x-axis. By default -4 degrees. This should not need changed unless you are using custom satellite spot patterns.

### Calibration

```toml
[calibration]
```

This section will enable standard image calibration of VAMPIRES data. The following steps will occur

1. If dark files are provided, a master dark will be made
2. If flat files are provided, a master flat will be made
3. The FITS headers will be fixed using `fix_header`
4. The leading 2 frames of each data cube will be discarded due to detector readout artifacts
5. If dark files are provided, the cube will be dark subtracted
6. If flat files are provided, the cube will be flat normalized
7. Cam 1 data will be flipped along the y-axis
8. (Advanced) Interleaved polarimetric data will be deinterleaved into two cubes

```toml
filenames = [""] # list of filenames
filenames = "input_files.txt" # the path to a text file 
filenames = "science/VMPA*.fits" # python glob expression 
```

The `filenames` option can either be a list of filenames, a path to a text file that contains a single filename per row, or a glob expression compatible with [Python's glob](https://docs.python.org/3/library/pathlib.html#pathlib.Path.glob).

```toml
output_directory = "" # relative to root, optional
```
The output directory for the calibrated data. By default, will leave in the same directory as the input data (the root directory).

```toml
force = false # optional
```
By default, if the output file already exists the calibration will be skipped to save time. If you set this to `true`, the calibration _and all subsequent operations_ will be redone.

```toml
deinterleave = false # optional
```
This is an advanced option for polarimetric data that is downloaded directly from the VAMPIRES computer (i.e., not from the STARS archive). If true, will deinterleave every-other frame into two cubes and will update the FITS header with the `U_FLCSTT` key.

```{admonition} Advanced Usage
:class: warning
If your data starts with `VMPA*.fits` then you should not set this to true!
```

```toml
[calibration.darks]
```
If this section is set, master dark frames will be created.

```toml
filenames = [
    "darks_5ms_em300_cam1.fits",
    "darks_5ms_em300_cam2.fits"
] # list of filenames
filenames = "input_darks.txt" # the path to a text file 
filenames = "darks/VMPA*.fits" # python glob expression 
```

The `filenames` option can either be a list of filenames (one for cam 1 and one for cam 2), a path to a text file that contains a single filename per row, or a glob expression compatible with [Python's glob](https://docs.python.org/3/library/pathlib.html#pathlib.Path.glob).

```toml
force = false # optional
```
By default, if the master dark already exists the calibration will be skipped to save time. If you set this to `true`, the master dark _and all subsequent operations_ will be redone.


```toml
[calibration.flats]
```
If this section is set, master flat frames will be created. If `[calibration.darks]` is also set, these flat frames will be dark-subtracted.

```toml
filenames = [
    "flats_em300_cam1.fits",
    "flats_em300_cam2.fits"
] # list of filenames
filenames = "input_flats.txt" # the path to a text file 
filenames = "flats/VMPA*.fits" # python glob expression 
```

The `filenames` option can either be a list of filenames (one for cam 1 and one for cam 2), a path to a text file that contains a single filename per row, or a glob expression compatible with [Python's glob](https://docs.python.org/3/library/pathlib.html#pathlib.Path.glob).

```toml
force = false # optional
```
By default, if the master flat already exists the calibration will be skipped to save time. If you set this to `true`, the master flat _and all subsequent operations_ will be redone.

### Outputs

FITS files will be saved to the `output_directory` with `_calib` appended to the name. If `deinterleave` is true, the files will also have either `_FLC1` or `_FLC2` appended.

### Frame Selection

```toml
[frame_selection] # optional
```

Frame selection is an optional step that can measure the image quality metrics and optionally discard frames with metrics below a certain quantile.

```toml
metric = "l2norm" # optional
```

Frame selection metric, one of "max", "l2norm", or "normvar". By default "l2norm".

```toml
q = 0 # optional
```

Frame selection quantile [0, 1). A value of 0 means no frames will be discarded (and we skip the step), and a value of 1 would discard all the frames. For example, to discard the lowest scoring 30% of frames based on the frame selection metric set `q = 0.3`.

```toml
window_size = 30 # pixels, optional
```

The frame selection metric is measured in a window for speed. In non-coronagraphic data this is a window around the frame center, and in coronagraphic data this is the window around each satellite spot.

```toml
output_directory = "" # relative to root, optional
```
The output directory for the metrics and frame-selected data. By default, will leave in the same directory as the input data (the root directory).

```
force = false # optional
```
By default, if the frame selection metrics or the frame-selected data cubes already exist the operations be skipped to save time. If you set this to `true`, the metric measurements, discarding, _and all subsequent operations_ will be redone.

#### Outputs

CSV files will be saved in `output_directory` with `_metric` appended to the file name with the frame selection metrics for each frame in the data cube. FITS files will be saved with `_cut` appended to the file name if frames are discarded (`q` > 0).

### Image Registration

```toml
[registration] # optional
```

Image registration is an optional step that can measure the offset of the stellar PSF from the geometric frame center and co-align the frames.

```toml
method = "com" # optional
```

Registration offset measurement method, one of "peak", "com", "dft", "moffat", "airydisk", "gaussian". By default "com" for coronagraphic data and "peak" for non-coronagraphic data.

```toml
[registration.dft] # optional
upsample_factor = 1 # optional
reference_method = "com" # optional
```

Extra options for the cross-correlation registration method. See `measure_offsets` for more details.

```toml
window_size = 30 # pixels, optional
```

The PSF offsets are measured in a window for speed. In non-coronagraphic data this is a window around the frame center, and in coronagraphic data this is the window around each satellite spot.


```toml
output_directory = "" # relative to root, optional
```
The output directory for the offsets and aligned data. By default, will leave in the same directory as the input data (the root directory).

```
force = false # optional
```
By default, if the offsets or the aligned data cubes already exist the operations be skipped to save time. If you set this to `true`, the offset measurements, alignment, _and all subsequent operations_ will be redone.

#### Outputs

CSV files will be saved in `output_directory` with `_offsets` appended to the file name with the PSF offsets (y, x) for each frame in the data cube. FITS files will be saved with `_aligned` appended to the file name.

### Coadding

```toml
[coadd] # optional
```

If this section is set, the data cubes will be median-combined along the time axis.

```toml
output_directory = "" # relative to root, optional
```
The output directory for the collapsed data. By default, will leave in the same directory as the input data (the root directory).

```
force = false # optional
```
By default, if the collapsed data frames already exist the operations be skipped to save time. If you set this to `true`, the collapsing, _and all subsequent operations_ will be redone.

#### Outputs

FITS files will be saved in `output_directory` with `_collapsed` appended to the file name.

### Derotation

```toml
[derotate] # optional
```

If this section is set, the collapsed data will be derotated to North up, East left.

```toml
pupil_offset = 140.4 # deg, optional
```

The pupil offset between the IR bench and the Visible bench. This should not need to be changed unless you have a custom astrometric solution.

```toml
output_directory = "" # relative to root, optional
```

The output directory for the derotated data. By default, will leave in the same directory as the input data (the root directory).

```
force = false # optional
```
By default, if the derotated data frames already exist the operations be skipped to save time. If you set this to `true`, the derotation, _and all subsequent operations_ will be redone.

### Outputs

```toml
output_directory = "" # relative to root, optional
```
The output directory for the derotated data. By default, will leave in the same directory as the input data (the root directory).

```
force = false # optional
```
By default, if the derotated data frames already exist the operations be skipped to save time. If you set this to `true`, the derotation, _and all subsequent operations_ will be redone.

#### Outputs

FITS files will be saved in `output_directory` with `_derot` appended to the file name.