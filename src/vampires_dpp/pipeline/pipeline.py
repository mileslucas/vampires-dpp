import multiprocessing as mp
import warnings
from functools import partial
from pathlib import Path

import numpy as np
import pandas as pd
import tomli
from astropy.io import fits
from loguru import logger
from tqdm.auto import tqdm

from vampires_dpp.analysis import analyze_file
from vampires_dpp.calib.calib_files import match_calib_file
from vampires_dpp.calib.calibration import calibrate_file
from vampires_dpp.coadd import coadd_hdul, collapse_frames
from vampires_dpp.combine_frames import (
    combine_frames_files,
    combine_frames_headers,
    combine_hduls,
    generate_frame_combinations,
)
from vampires_dpp.frame_select import frame_select_hdul
from vampires_dpp.image_registration import register_hdul
from vampires_dpp.organization import header_table
from vampires_dpp.paths import Paths, get_paths, get_reduced_path, make_dirs
from vampires_dpp.pdi.diff_images import (
    doublediff_images,
    get_doublediff_sets,
    get_singlediff_sets,
    singlediff_images,
)
from vampires_dpp.pdi.models import mueller_matrix_from_file
from vampires_dpp.pdi.processing import get_doublediff_set, get_triplediff_set, make_stokes_image
from vampires_dpp.pdi.utils import write_stokes_products
from vampires_dpp.pipeline.config import PipelineConfig
from vampires_dpp.specphot.filters import determine_filterset_from_header
from vampires_dpp.specphot.specphot import specphot_cal_hdul
from vampires_dpp.synthpsf import create_synth_psf
from vampires_dpp.wcs import apply_wcs


class Pipeline:
    def __init__(self, config: PipelineConfig, workdir: Path | None = None):
        self.master_backgrounds = {1: None, 2: None}
        self.master_flats = {1: None, 2: None}
        self.diff_files = None
        self.calib_table = None
        self.config = config
        self.workdir = workdir if workdir is not None else Path.cwd()
        self.paths = Paths(workdir=self.workdir)
        self.output_table_path = self.paths.aux / f"{self.config.name}_table.csv"

    def run(self, filenames, num_proc: int | None = None, force=False):
        """Run the pipeline

        Parameters
        ----------
        filenames : Iterable[PathLike]
            Input filenames to process
        num_proc : Optional[int]
            Number of processes to use for multi-processing, by default None.
        """
        make_dirs(self.paths, self.config)
        conf_copy_path = self.paths.aux / f"{self.config.name}.bak.toml"
        self.config.save(conf_copy_path)
        logger.debug(f"Saved copy of config to {conf_copy_path}")

        input_table = self.create_input_table(filenames=filenames, num_proc=num_proc)
        self.get_centroids()
        self.get_coordinate()
        self.make_synth_psfs(input_table)
        combinations = generate_frame_combinations(input_table, method=self.config.combine.method)
        input_table["GROUP_IDX"] = combinations["GROUP_IDX"]

        if self.config.calibrate.calib_directory is not None:
            self.calib_table = header_table(
                self.config.calibrate.calib_directory.glob("**/[!.]*.fits"), quiet=True
            )

        self.output_paths = []
        with mp.Pool(num_proc) as pool:
            jobs = []
            for group_index, group in input_table.groupby("GROUP_IDX"):
                output_path = get_reduced_path(self.paths, self.config, group_index)
                if not force and output_path.exists():
                    logger.debug("Skipping processing for group %3d", output_path)
                    self.output_paths.append(output_path)
                else:
                    jobs.append(
                        pool.apply_async(self.process_group, args=(group, group_index, output_path))
                    )

            for job in tqdm(jobs, desc="Processing files"):
                self.output_paths.append(job.get())
        self.output_paths.sort()

        logger.info("Creating table from collapsed headers")
        self.output_table = header_table(self.output_paths, num_proc=num_proc, quiet=True)
        self.save_output_header()

        # ## products
        # if self.config.save_adi_cubes:
        #     self.save_adi_cubes(force=force)

        # ## diff images
        # if self.config.diff_images.make_diff:
        #     self.make_diff_images(self.output_table, force=force)

        logger.success("Finished processing files")

    def run_polarimetry(self, num_proc, force=False):
        make_dirs(self.paths, self.config)
        conf_copy_path = self.paths.aux / f"{self.config.name}.bak.toml"
        self.config.save(conf_copy_path)
        logger.debug(f"Saved copy of config to {conf_copy_path}")

        if not self.output_table_path.exists():
            msg = f"Output table {self.output_table_path} cannot be found"
            raise RuntimeError(msg)

        working_table = pd.read_csv(self.output_table_path, index_col=0).sort_values("MJD")

        if self.config.polarimetry.mm_correct or self.config.polarimetry.method == "leastsq":
            working_table["mm_file"] = self.make_mueller_mats(working_table, num_proc=num_proc)

        logger.info("Performing polarimetric calibration")
        logger.debug(f"Saving Stokes data to {self.paths.pdi.absolute()}")
        match self.config.polarimetry.method:
            case "doublediff" | "triplediff":
                self.polarimetry_difference(
                    working_table,
                    method=self.config.polarimetry.method,
                    force=force,
                    num_proc=num_proc,
                )
            case "leastsq":
                self.polarimetry_leastsq(working_table, force=force, num_proc=num_proc)
        logger.success("Finished PDI")

    def create_input_table(self, filenames, num_proc) -> pd.DataFrame:
        input_table = header_table(filenames, quiet=True, num_proc=num_proc).sort_values("MJD")
        table_path = self.paths.aux / f"{self.config.name}_input_headers.csv"
        input_table.to_csv(table_path)
        logger.info(f"Saved input header table to: {table_path}")
        return input_table

    def get_centroids(self):
        self.centroids = {}
        for key in ("cam1", "cam2"):
            path = self.paths.aux / f"{self.config.name}_centroids_{key}.toml"
            if not path.exists():
                logger.warning(
                    f"Could not locate centroid file for {key}, expected it to be at {path}. Using center of image as default."
                )
                continue
            with path.open("rb") as fh:
                centroids = tomli.load(fh)
            self.centroids[key] = {}
            for field, ctrs in centroids.items():
                self.centroids[key][field] = np.flip(np.atleast_2d(ctrs), axis=-1)

            logger.debug(f"{key} frame center is {self.centroids[key]} (y, x)")
        return self.centroids

    def make_synth_psfs(self, input_table):
        # make PSFs ahead of time so they don't overwhelm
        # during multiprocessing
        filters = {}
        for _, row in input_table.iterrows():
            for filt in determine_filterset_from_header(row):
                filters[filt] = row

        self.synth_psfs = {}
        for filt, row in filters.items():
            psf = create_synth_psf(
                row, filt, npix=self.config.analysis.window_size, output_directory=self.paths.aux
            )
            self.synth_psfs[filt] = psf

    def process_group(self, group, index: int, output_path: Path):
        # fix headers and calibrate
        hdul_list = []
        for _, row in group.iterrows():
            logger.debug(f"Calibrating {row['path']}")
            cur_hdul = self.calibrate_one(row["path"], row)
            hdul_list.append(cur_hdul)
        logger.debug("Finished calibrating %d files", len(group))
        logger.debug("Combining data into single HDU list")
        hdul = combine_hduls(hdul_list)
        ## Step 2: Frame analysis
        metric_file = self.paths.metrics / f"{self.config.name}_{index:03d}_metrics.npz"
        metrics = self.analyze_one(hdul, metric_file)
        ## Step 3: Frame selection
        if self.config.frame_select.frame_select:
            logger.debug("Starting frame selection for group %3d", index)
            hdul, metrics = frame_select_hdul(
                hdul,
                metrics,
                metric=self.config.frame_select.metric,
                quantile=self.config.frame_select.cutoff,
            )
            logger.debug("Finished frame selection for group %3d", index)
        ## Step 4: Registration
        # note: if we're not aligning, this still takes care
        # of cutting out MBI frames, so it's necessary
        logger.debug("Starting frame registration for group %3d", index)
        hdul = register_hdul(
            hdul,
            metrics,
            align=self.config.align.align,
            method=self.config.align.method,
            crop_width=self.config.align.crop_width,
        )
        logger.debug("Finished frame registration for group %3d", index)

        ## Step 5: Spectrophotometric calibration
        logger.debug("Starting specphot cal for group %3d", index)
        hdul = specphot_cal_hdul(hdul, metrics=metrics, config=self.config.specphot)
        logger.debug("Finished specphot cal for group %3d", index)
        ## Step 6: Coadd
        if self.config.coadd.coadd:
            logger.debug("Starting coadding for group %3d", index)
            psfs = [
                self.synth_psfs[filt] for filt in determine_filterset_from_header(hdul[0].header)
            ]
            hdul = coadd_hdul(
                hdul,
                method=self.config.coadd.method,
                recenter=self.config.coadd.recenter,
                recenter_method=self.config.coadd.recenter_method,
                dft_factor=self.config.coadd.recenter_dft_factor,
                psfs=psfs,
            )
            logger.debug("Finished coadding for group %3d", index)

        logger.debug("Saving reduced cube to %s", str(output_path))
        hdul.writeto(output_path, overwrite=True)

        return output_path

    def get_coordinate(self):
        if self.config.target is None:
            self.coord = None
        else:
            self.coord = self.config.target.get_coord()

    def calibrate_one(self, path, fileinfo, force=False):
        logger.debug("Starting data calibration")
        config = self.config.calibrate
        if config.save_intermediate:
            outpath = get_paths(
                path, suffix="calib", filetype=".fits", output_directory=self.paths.calibrated
            )[1]
            if not force and outpath.exists():
                return fits.open(outpath)

        back_filename = None
        flat_filename = None
        if self.calib_table is not None:
            calib_match = match_calib_file(path, self.calib_table)
            if config.back_subtract:
                back_filename = calib_match["backfile"]
            if config.flat_correct:
                flat_filename = calib_match["flatfile"]
        calib_hdul = calibrate_file(
            path,
            back_filename=back_filename,
            flat_filename=flat_filename,
            # transform_filename=config.distortion_file,
            bpfix=config.fix_bad_pixels,
            coord=self.coord,
            force=force,
        )
        if config.save_intermediate:
            calib_hdul.writeto(outpath, overwrite=True)
            logger.debug(f"Calibrated data saved to {outpath}")
        logger.debug("Data calibration completed")
        return calib_hdul

    def analyze_one(self, hdul: fits.HDUList, metric_file, force=False):
        logger.debug("Starting frame analysis")
        if not force and metric_file.exists():
            return np.load(metric_file)
        config = self.config.analysis
        hdr = hdul[0].header
        if self.config.align.align and self.config.align.method == "dft":
            psfs = [self.synth_psfs[filt] for filt in determine_filterset_from_header(hdr)]
            dft_factor = self.config.align.dft_factor
        else:
            psfs = None
            dft_factor = -1
        key = f"cam{hdr['U_CAMERA']:.0f}"
        outpath = analyze_file(
            hdul,
            centroids=self.centroids.get(key, None),
            window_size=config.window_size,
            aper_rad=config.phot_aper_rad,
            ann_rad=config.phot_ann_rad,
            psfs=psfs,
            dft_factor=dft_factor,
            do_phot=config.photometry,
            fit_psf_model=config.fit_psf_model,
            psf_model=config.psf_model,
            outpath=metric_file,
            force=force,
        )
        return np.load(outpath)

    def save_output_header(self):
        self.output_table.to_csv(self.output_table_path)
        logger.info(f"Saved output header table to {self.output_table_path}")
        return self.output_table_path

    def save_adi_cubes(self, force: bool = False):
        # preset values
        self.cam1_cube_path = self.cam2_cube_path = None

        # save cubes for each camera
        if "U_FLC" in self.output_table:
            self.output_table.sort_values(["MJD", "U_FLC"], inplace=True)
        else:
            self.output_table.sort_values("MJD", inplace=True)

        hduls = []
        cam_groups = self.output_table.groupby("U_CAMERA")
        for cam_num, group in cam_groups:
            cube_path = self.paths.adi / f"{self.config.name}_adi_cube_cam{cam_num:.0f}.fits"
            combine_frames_files(group["path"], output=cube_path, force=force, crop=False)
            hduls.append(fits.open(cube_path, memmap=False))
            logger.info(f"Saved cam {cam_num:.0f} ADI cube to {cube_path}")
            angles_path = cube_path.with_stem(f"{cube_path.stem}_angles")
            angles = np.asarray(group["DEROTANG"], dtype="f4")
            fits.writeto(angles_path, angles, overwrite=True)
            logger.info(f"Saved cam {cam_num:.0f} ADI angles to {angles_path}")
        # take cam 1 as refernce, why not
        # if len(hduls) > 1:
        #     # cam1_cube, cam1_hdr, cam2_cube, cam2_hdr = reproject_data(
        #     #     hduls[0][0].data, hduls[0][0].header, hduls[1][0].data, hduls[1][0].header
        #     # )
        #     cam1_cube = hduls[0][0].data
        #     cam1_hdr = hduls[0][0].header
        #     cam2_cube = hduls[1][0].data
        #     # cam2_hdr = hduls[1][0].header

        #     mean_cube = 0.5 * (cam1_cube + cam2_cube)
        #     cube_path = self.paths.adi_dir / f"{self.config.name}_adi_cube_comb.fits"
        #     del cam1_hdr["U_CAMERA"]
        #     fits.writeto(cube_path, mean_cube, header=cam1_hdr, overwrite=True)
        #     logger.info(f"Saved combined ADI cube to {cube_path}")
        #     angles_path = cube_path.with_stem(f"{cube_path.stem}_angles")
        #     angles = np.asarray(cam_groups.get_group(1)["DEROTANG"])
        #     fits.writeto(angles_path, angles, overwrite=True)
        #     logger.info(f"Saved combined ADI angles to {angles_path}")

    def make_diff_images(self, table, num_proc=None, force=False):
        logger.info("Making difference frames")
        self.diff_files = []
        if self.config.diff_images == "singlediff":
            path_sets = get_singlediff_sets(table)
            diff_func = partial(singlediff_images, force=force)
            outdir = self.paths.diff / "single"
        elif self.config.diff_images == "doublediff":
            path_sets = get_doublediff_sets(table)
            diff_func = partial(doublediff_images, force=force)
            outdir = self.paths.diff / "double"
        elif self.config.diff_images == "both":
            # do singlediff first, then deliberate to doublediff
            path_sets = get_singlediff_sets(table)
            diff_func = partial(singlediff_images, force=force)
            outdir = self.paths.diff / "single"
            outdir.mkdir(exist_ok=True)
            with mp.Pool(num_proc) as pool:
                jobs = []
                for i, paths in enumerate(path_sets):
                    outpath = outdir / f"{self.config.name}_diff_{i:04d}.fits"
                    jobs.append(
                        pool.apply_async(diff_func, args=(paths,), kwds=dict(outpath=outpath))
                    )
                self.diff_files.extend(
                    job.get() for job in tqdm(jobs, desc="Making single diff images")
                )
            # now set for double-diff
            path_sets = get_doublediff_sets(table)
            diff_func = partial(doublediff_images, force=force)
            outdir = self.paths.diff / "double"
        outdir.mkdir(exist_ok=True)
        with mp.Pool(num_proc) as pool:
            jobs = []
            for i, paths in enumerate(path_sets):
                outpath = outdir / f"{self.config.name}_diff_{i:04d}.fits"
                jobs.append(pool.apply_async(diff_func, args=(paths,), kwds=dict(outpath=outpath)))

            self.diff_files.extend(job.get() for job in tqdm(jobs, desc="Making diff images"))
        logger.info("Done making difference frames")
        return self.diff_files

    def make_mueller_mats(self, table, num_proc=None, force=False):
        logger.info("Creating Mueller matrices")
        mm_paths = []
        kwds = dict(
            hwp_adi_sync=self.config.polarimetry.hwp_adi_sync,
            ideal=self.config.polarimetry.use_ideal_mm,
            force=force,
        )
        with mp.Pool(num_proc) as pool:
            jobs = []
            for row in table.itertuples(index=False):
                _, outpath = get_paths(row.path, suffix="mm", output_directory=self.paths.mm)
                jobs.append(
                    pool.apply_async(mueller_matrix_from_file, args=(row.path, outpath), kwds=kwds)
                )

            for job in tqdm(jobs, desc="Making Mueller matrices"):
                mm_paths.append(job.get())

        return mm_paths

    def polarimetry_difference(self, table, method, num_proc=None, force=False):
        config = self.config.polarimetry
        stokes_sets_path = self.paths.pdi / f"{self.config.name}_stokes_sets.csv"
        if stokes_sets_path.exists():
            stokes_sets = pd.read_csv(stokes_sets_path)
            logger.info(f"Loaded HWP cycle combinations from {stokes_sets_path}")
        else:
            match method.lower():
                case "triplediff":
                    stokes_sets = get_triplediff_set(table)
                case "doublediff":
                    stokes_sets = get_doublediff_set(table)
                case _:
                    msg = f"Invalid polarimetric difference method '{method}'"
                    raise ValueError(msg)
            stokes_sets.to_csv(stokes_sets_path, index=False)
            logger.info(f"Saved HWP cycle combinations to {stokes_sets_path}")

        ## Save CSV of Stokes values
        stokes_tbl = header_table(stokes_sets["path"], fix=False, quiet=True)
        stokes_tbl_path = self.paths.pdi / f"{self.config.name}_stokes_table.csv"
        stokes_tbl.to_csv(stokes_tbl_path)
        logger.info(f"Saved table of Stokes file headers to {stokes_tbl_path}")

        stokes_data = []
        stokes_err = []
        stokes_hdrs = []
        stokes_func = partial(
            make_stokes_image,
            method=method,
            mm_correct=config.mm_correct,
            hwp_adi_sync=config.hwp_adi_sync,
            ip_correct=config.ip_correct,
            ip_method=config.ip_method,
            ip_radius=config.ip_radius,
            ip_radius2=config.ip_radius2,
            force=force,
        )
        # TODO this is kind of ugly
        with mp.Pool(num_proc) as pool:
            jobs = []
            for set_idx, group in stokes_sets.query("STOKES_IDX != -1").groupby("STOKES_IDX"):
                paths = group["path"]
                outpath = self.paths.stokes / f"{self.config.name}_stokes_{set_idx:03d}.fits"
                if config.mm_correct:
                    mask = [p in paths.values for p in table["path"]]
                    subset = table.loc[mask]
                    mm_paths = subset["mm_file"]
                else:
                    mm_paths = None
                if len(paths) != (16 if method == "triplediff" else 8):
                    continue
                jobs.append(pool.apply_async(stokes_func, args=(paths, outpath, mm_paths)))

            for job in tqdm(jobs, desc="Creating Stokes images"):
                outpath = job.get()
                # use memmap=False to avoid "too many files open" effects
                # another way would be to set ulimit -n <MAX_FILES>
                with fits.open(outpath, memmap=False) as hdul:
                    stokes_data.append(hdul[0].data)
                    stokes_err.append(hdul["ERR"].data)
                    hdrs = [hdul[i].header for i in range(2, len(hdul))]
                    stokes_hdrs.append(hdrs)
        ## Collapse outputs
        logger.info(f"Collapsing {len(stokes_tbl)} Stokes files...")
        stokes_data = np.array(stokes_data)
        stokes_err = np.array(stokes_err)
        coll_frame, _ = collapse_frames(np.nan_to_num(stokes_data))
        footprint = np.mean(np.isfinite(stokes_data).astype("f4"), axis=0)
        fits.writeto(
            self.paths.pdi / f"{self.config.name}_footprint.fits", footprint, overwrite=True
        )
        # coll_frame *= footprint
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            coll_err = np.sqrt(np.nansum(stokes_err**2, axis=0)) / stokes_err.shape[0]
        nfields = len(stokes_hdrs[0])
        coll_hdrs = []
        for i in range(nfields):
            hdrs = [hdr[i] for hdr in stokes_hdrs]
            hdr = apply_wcs(coll_frame, combine_frames_headers(hdrs), angle=0)
            coll_hdrs.append(hdr)

        # correct TINT to account for actual number of files used
        tints = [fits.getval(path, "TINT") for path in np.unique(stokes_sets["path"])]
        tint = np.sum(tints)
        for hdr in coll_hdrs:
            hdr["NCOADD"] = len(tints)
            hdr["TINT"] = tint
        prim_hdr = apply_wcs(coll_frame, combine_frames_headers(coll_hdrs), angle=0)
        prim_hdr["NCOADD"] /= len(coll_hdrs)
        prim_hdr["TINT"] /= len(coll_hdrs)
        prim_hdu = fits.PrimaryHDU(coll_frame, header=prim_hdr)
        err_hdu = fits.ImageHDU(coll_err, header=prim_hdr, name="ERR")
        hdul = fits.HDUList([prim_hdu, err_hdu])
        hdul.extend([fits.ImageHDU(header=hdr, name=hdr["FIELD"]) for hdr in coll_hdrs])
        # In the case we have multi-wavelength data, save 4D Stokes cube
        if nfields > 1:
            stokes_cube_path = self.paths.pdi / f"{self.config.name}_stokes_cube.fits"
            write_stokes_products(
                hdul, outname=stokes_cube_path, force=True, planetary=config.planetary
            )
            logger.info(f"Saved Stokes cube to {stokes_cube_path}")

            # now collapse wavelength axis and clobber
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                wave_coll_frame = np.nansum(coll_frame, axis=0, keepdims=True)
                wave_err_frame = np.sqrt(np.nansum(coll_err**2, axis=0, keepdims=True))
            wave_coll_hdr = apply_wcs(wave_coll_frame, combine_frames_headers(coll_hdrs), angle=0)
            wave_coll_hdr["NCOADD"] /= len(coll_hdrs)
            wave_coll_hdr["TINT"] /= len(coll_hdrs)
            wave_coll_hdr["FIELD"] = "COMB"
            # TODO some fits keywords here are screwed up
            prim_hdu = fits.PrimaryHDU(wave_coll_frame, header=wave_coll_hdr)
            err_hdu = fits.ImageHDU(wave_err_frame, header=wave_coll_hdr, name="ERR")
            dummy_hdu = fits.ImageHDU(header=wave_coll_hdr, name="COMB")
            hdul = fits.HDUList([prim_hdu, err_hdu, dummy_hdu])
        # save single-wavelength (or wavelength-collapsed) Stokes cube
        stokes_coll_path = self.paths.pdi / f"{self.config.name}_stokes_coll.fits"
        write_stokes_products(
            hdul, outname=stokes_coll_path, force=True, planetary=config.planetary
        )
        logger.info(f"Saved collapsed Stokes cube to {stokes_coll_path}")

    def polarimetry_leastsq(self, table, force=False):
        msg = "Need to rewrite this, sorry."
        raise NotImplementedError(msg)
        # self.stokes_collapsed_file = self.paths.pdi_dir / f"{self.config.name}_stokes_coll.fits"
        # if (
        #     force
        #     or not self.stokes_collapsed_file.is_file()
        #     or any_file_newer(self.working_db["path"], self.stokes_collapsed_file)
        # ):
        #     # create stokes cube
        #     polarization_calibration_leastsq(
        #         self.working_db["path"],
        #         self.working_db["mm_file"],
        #         outname=self.stokes_collapsed_file,
        #         force=True,
        #     )
