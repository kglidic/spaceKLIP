import numpy as np
from scipy.ndimage.interpolation import rotate
import webbpsf_ext
webbpsf_ext.setup_logging('WARN', verbose=False)

from webbpsf_ext.webbpsf_ext_core import _transmission_map
from webbpsf_ext.image_manip import frebin, fourier_imshift
from webbpsf_ext.image_manip import pad_or_cut_to_size
from webbpsf_ext.coords import rtheta_to_xy
from webbpsf_ext import NIRCam_ext, MIRI_ext

import pysynphot as S

# Progress bar
from tqdm.auto import trange, tqdm

class JWST_PSF():
    
    def __init__(self, inst, filt, image_mask, fov_pix, oversample=2, 
                 sp=None, use_coeff=True, date=None, **kwargs): 
        """ Class to generate off-axis coronagraphic PSF

        This object provides the ability to generate a synthetic NIRCam coronagraphic
        PSF using webbpsf and webbpsf_ext at an arbitrary location relative to the 
        occulting mask, taking into account mask attentuation near the IWA.

        There are multiple ways to estimate these PSFs, either through extrapolation
        from the theoretical occulting mask transmission (fastest), using the 
        webbpsf_ext PSF coefficients (intermediate speed), or on-the-fly calculations
        using webbpsf (slowest, but most accurate). 

        Includes the ability to use date-specific OPD maps as generated by the JWST
        wavefront sensing group. Simply set use_coeff=False, and supply a date in ISO 
        format.

        All resulting PSFs were normalized such that their total intensity is 1.0
        at the telescope entrance pupil. So, the final intensity of these PSFs
        include throughput attentuation at intermediate optics such as the NIRCam
        Lyot stops in the pupil wheels. 

        Parameters
        ==========
        inst : str
            Instrument name either 'NIRCAM' or 'MIRI'.
        filter : str
            NIRCam filter (e.g., F335M)
        image_mask : str
            NIRCam coronagraphic occulting mask (e.g., MASK335R)
        fov_pix : int
            PSF pixel size. Suggest odd values for centering PSF in middle of a pixel
            rather than pixel corner / boundaries.
        oversample : int
            Size of oversampling.
        sp : pysynclphot spectrum
            Spectrum to use for PSF wavelenght weighting. If None, then default is G2V.
        use_coeff : bool
            Generate PSFs from webbpsf_ext coefficient library. If set to False, then
            will use webbpsf to generate PSFs on-the-fly, opening up the ability to
            use date-specific OPD files via the `date` keyword.
        date : str or None
            Date time in UTC as ISO-format string, a la 2022-07-01T07:20:00.
            If not set, then default webbpsf OPD is used (e.g., RevAA).
        """
        # Assign extension to use based on instrument
        if inst.upper() == 'NIRCAM':
            self.inst_ext = NIRCam_ext
        elif inst.upper() == 'MIRI':
            self.inst_ext = MIRI_ext

        # Choose Lyot stop based on coronagraphic mask input
        if image_mask is None:
            pupil_mask = None
        elif image_mask[-1] == 'R':
            pupil_mask = 'CIRCLYOT'
        elif image_mask[-1] == 'B':
            pupil_mask = 'WEDGELYOT'
        else:
            pupil_mask = 'MASKFQPM' if 'FQPM' in image_mask else 'MASKLYOT'
            
        inst_on = self.inst_ext(filter=filt, image_mask=image_mask, pupil_mask=pupil_mask,
                                 fov_pix=fov_pix, oversample=oversample, **kwargs)

        inst_off = self.inst_ext(filter=filt, image_mask=None, pupil_mask=pupil_mask,
                                  fov_pix=fov_pix, oversample=oversample, **kwargs)

        # Load date-specific OPD files?
        if date is not None:
            inst_on.load_wss_opd_by_date(date=date, choice='closest', verbose=False, plot=False)
            inst_off.load_wss_opd_by_date(date=date, choice='closest', verbose=False, plot=False)
        
        # Generating initial PSFs...
        print('Generating initial PSFs...')
        if use_coeff:
            inst_on.options['jitter_sigma'] = 0
            inst_off.options['jitter_sigma'] = 0
            inst_on.gen_psf_coeff()
            inst_off.gen_psf_coeff()
            func_on = inst_on.calc_psf_from_coeff
            func_off = inst_off.calc_psf_from_coeff
            inst_on.gen_wfemask_coeff(large_grid=True)
        else:
            func_on = inst_on.calc_psf
            func_off = inst_off.calc_psf
            inst_on.options['jitter'] = 'gaussian'
            inst_on.options['jitter_sigma'] = 0.00#3
            inst_off.options['jitter'] = 'gaussian'
            inst_off.options['jitter_sigma'] = 0.00#3

        # Renormalize spectrum to have 1 e-/sec within bandpass to obtain normalized PSFs
        if sp is not None:
            try:
                sp = sp.renorm(1, 'counts', inst_on.bandpass)
            except:
                # Our spectrum was probably made in synphot not pysynphot, 
                wunit = sp.waveset.unit.to_string()
                funit = sp(sp.waveset).unit.to_string()
                sp = S.ArraySpectrum(sp.waveset.value, sp(sp.waveset).value, wunit, funit, name=sp.meta['name'])
                sp = sp.renorm(1, 'counts', inst_on.bandpass)

        # On axis PSF
        if image_mask[-1] == 'B':
            # Need an array of PSFs along bar center
            xvals = np.linspace(-8,8,9)
            self.psf_bar_xvals = xvals
            
            psf_bar_arr = []
            for xv in tqdm(xvals, desc='Bar PSFs', leave=False):
                psf = func_on(sp=sp, return_oversample=True, return_hdul=False, 
                              coord_vals=(xv,0), coord_frame='idl')
                psf_bar_arr.append(psf)
            self.psf_on = np.array(psf_bar_arr)
        else:
            self.psf_on = func_on(sp=sp, return_oversample=True, return_hdul=False)
            
        # Off axis PSF
        self.psf_off = func_off(sp=sp, return_oversample=True, return_hdul=False)

        # Center PSFs
        self._recenter_psfs()

        # Store instrument classes
        self.inst_on  = inst_on
        self.inst_off = inst_off
        
        # PSF generation functions for later use
        self._use_coeff = use_coeff
        self._func_on  = func_on
        self._func_off = func_off

        self.sp = sp
        
    @property
    def fov_pix(self):
        return self.inst_on.fov_pix
    @property
    def oversample(self):
        return self.inst_on.oversample

    @property
    def filter(self):
        return self.inst_on.filter
    @property
    def image_mask(self):
        return self.inst_on.image_mask
    @property
    def pupil_mask(self):
        return self.inst_on.pupil_mask
    
    @property
    def use_coeff(self):
        return self._use_coeff

    @property
    def bandpass(self):
        return self.inst_on.bandpass

    def _calc_psf_off_shift(self, xysub=10):
        """Calculate oversampled pixel shifts using off-axis PSF and Gaussian centroiding"""

        from astropy.modeling import models, fitting

        xv = yv = np.arange(xysub)
        xgrid, ygrid = np.meshgrid(xv, yv)
        xc, yc = (xv.mean(), yv.mean())

        psf_template = pad_or_cut_to_size(self.psf_off, xysub+10)

        xoff = 0
        yoff = 0
        for ii in range(2):
            psf_off = pad_or_cut_to_size(psf_template, xysub)

            # Fit the data using astropy.modeling
            p_init = models.Gaussian2D(amplitude=psf_off.max(), x_mean=xc, y_mean=yc, x_stddev=1, y_stddev=2)
            fit_p = fitting.LevMarLSQFitter()

            pfit = fit_p(p_init, xgrid, ygrid, psf_off)
            xcen_psf = xc - pfit.x_mean.value
            ycen_psf = yc - pfit.y_mean.value

            # Accumulate offsets
            xoff += xcen_psf
            yoff += ycen_psf

            # Update initial PSF location
            psf_template = fourier_imshift(psf_template, xcen_psf, ycen_psf, pad=True)

        # Save to attribute
        self._xy_off_to_cen = (xoff, yoff)

    
    def _recenter_psfs(self, **kwargs):
        """Recenter PSFs by centroiding on off-axis PSF and shifting both by same amount"""

        # Calculate shift
        self._calc_psf_off_shift(**kwargs)
        xoff, yoff = self._xy_off_to_cen
        
        # Perform recentering
        self.psf_on = fourier_imshift(self.psf_on, xoff, yoff, pad=True)
        self.psf_off = fourier_imshift(self.psf_off, xoff, yoff, pad=True)
        

    def _shift_psfs(self,shifts):
        """ Shift the on-axis and off-axis psfs by the desired amount
        
        Parameters
        ==========

        shifts : list of floats
                 The x and y offsets you want to apply [x,y]
        """
        xoff,yoff = shifts

        # Perform the shift
        self.psf_on = fourier_imshift(self.psf_on, xoff, yoff, pad=True)
        self.psf_off = fourier_imshift(self.psf_off, xoff, yoff, pad=True)
        self.xoff = xoff
        self.yoff = yoff
    
    def rth_to_xy(self, r, th, PA_V3=0, frame_out='idl', addV3Yidl=True):
        """ Convert (r,th) location to (x,y) in idl coords

        Assume (r,th) in coordinate system with North up East to the left.
        Then convert to NIRCam detector orientation (idl coord frame).
        Units assumed to be in arcsec.

        Parameters
        ==========
        r : float or ndarray
            Radial offst from mask center.
        th : float or ndarray
            Position angle (positive angles East of North) in degrees.
            Can also be an array; must match size of `r`.
        PA_V3 : float
            V3 PA of ref point N over E (e.g. 'ROLL_REF').
        frame_out : str
            Coordinate frame of output. Default is 'idl'

                * 'tel': arcsecs V2,V3
                * 'sci': pixels, in DMS axes orientation; aperture-dependent
                * 'det': pixels, in raw detector read out axes orientation
                * 'idl': arcsecs relative to aperture reference location.
        """

        # Convert to aperture PA
        if addV3Yidl == True:
            PA_ap = PA_V3 + self.inst_on.siaf_ap.V3IdlYAngle
        else:
            PA_ap = PA_V3
        # Get theta relative to detector orientation (idl frame)
        th_fin = th - PA_ap
        # Return (x,y) in idl frame
        xidl, yidl = rtheta_to_xy(r, th_fin)

        if frame_out=='idl':
            return (xidl, yidl)
        else:
            return self.inst_on.siaf_ap.convert(xidl, yidl, 'idl', frame_out)

    
    def gen_psf_idl(self, coord_vals, coord_frame='idl', quick=True, sp=None,
                    return_oversample=False, do_shift=False):
        """ Generate offset PSF in detector frame

        Generate a PSF with some (x,y) position in some coordinate frame (default idl).


        Parameters
        ==========
        coord_vals : tuple or None
            Coordinates (in arcsec or pixels) to calculate field-dependent PSF.
            If multiple values, then this should be an array ([xvals], [yvals]).
        coord_frame : str
            Type of input coordinates. Default is 'idl'

                * 'tel': arcsecs V2,V3
                * 'sci': pixels, in DMS axes orientation; aperture-dependent
                * 'det': pixels, in raw detector read out axes orientation
                * 'idl': arcsecs relative to aperture reference location.

        quick : bool
            Use linear combination of on-axis and off-axis PSFs to generate
            PSF as a function of corongraphic mask throughput. This is much
            faster (1 ms) than the standard calculations using coefficients (0.5 s) 
            or on-the-fly calcs w/ webbpsf (10 s).
        sp : pysynphot spectrum
            Manually specify spectrum to get a desired wavelength weighting. 
            Only applicable if ``quick=False``. If not set, defaults to ``self.sp``.
        return_oversample : bool
            Return the oversampled version of the PSF?
        do_shift : bool
            If True, will return the PSF offset from center. 
            Otherwise, returns PSF in center of image.
        """

        from scipy.interpolate import interp1d

        # Work with oversampled pixels and downsample at end
        siaf_ap = self.inst_on.siaf_ap
        osamp = self.inst_on.oversample
        ny = nx = self.fov_pix * osamp

        # Renormalize spectrum to have 1 e-/sec within bandpass to obtain normalized PSFs
        if sp is not None:
            sp = sp.renorm(1, 'counts', self.bandpass)
        
        if quick:
            t_temp, cx_idl, cy_idl = _transmission_map(self.inst_on, coord_vals, coord_frame)
            trans = t_temp**2

            # Linear combination of min/max to determine PSF
            # Get a and b values for each position
            avals = trans
            bvals = 1 - avals

            if self.image_mask[-1]=='B':
                # Interpolation function
                xvals = self.psf_bar_xvals
                psf_arr = self.psf_on
                finterp = interp1d(xvals, psf_arr, kind='linear', fill_value='extrapolate', axis=0)
                psf_on = finterp(cx_idl)
            else:
                psf_on = self.psf_on
            psf_off = self.psf_off

            psfs = avals.reshape([-1,1,1]) * psf_off.reshape([1,ny,nx]) \
                 + bvals.reshape([-1,1,1]) * psf_on.reshape([1,ny,nx])
        
        else:
            calc_psf = self._func_on
            sp = self.sp if sp is None else sp
            psfs = calc_psf(sp=sp, coord_vals=coord_vals, coord_frame=coord_frame,
                            return_oversample=True, return_hdul=False)

            # Ensure 3D cube
            psfs = psfs.reshape([-1,ny,nx])

            # Perform shift to center
            # Already done for quick case
            xoff, yoff = self._xy_off_to_cen
            psfs = fourier_imshift(psfs, xoff, yoff, pad=True)

        if do_shift:
            # Get offset in idl frame
            if coord_frame=='idl':
                xidl, yidl = coord_vals
            else:
                xidl, yidl = siaf_ap.convert(coord_vals[0], coord_vals[1], coord_frame, 'idl')

            # Convert to pixels for shifting
            dx_pix = np.array([osamp * xidl / siaf_ap.XSciScale]).ravel()
            dy_pix = np.array([osamp * yidl / siaf_ap.YSciScale]).ravel()

            psfs_sh = []
            for i, im in enumerate(psfs):
                psf = fourier_imshift(im, dx_pix[i], dy_pix[i], pad=True)
                psfs_sh.append(psf)
            psfs = np.asarray(psfs_sh)

        # Resample to detector pixels?
        if not return_oversample:
            psfs = frebin(psfs, scale=1/osamp)

        return psfs.squeeze()


    def gen_psf(self, loc, mode='xy', PA_V3=0, return_oversample=False, do_shift=True, addV3Yidl=True, normalize=False, **kwargs):
        """ Generate offset PSF rotated by PA to N-E orientation

        Generate a PSF for some (x,y) detector position in N-E sky orientation.

        Parameters
        ==========
        loc : float or ndarray
            (x,y) or (r,th) location (in arcsec) offset from center of mask.
        PA_V3 : float
            V3 PA of ref point N over E (e.g. 'ROLL_REF').
        return_oversample : bool
            Return the oversampled version of the PSF?
        do_shift : bool
            If True, will offset PSF by appropriate amount from center. Otherwise,
            returns PSF in center of image.

        Keyword Args
        ============
        quick : bool
            Use linear combination of on-axis and off-axis PSFs to generate
            PSF as a function of corongraphic mask throughput. This is much
            faster (1 ms) than the standard calculations using coefficients (0.5 s) 
            or on-the-fly calcs w/ webbpsf (10 s).
        sp : pysynphot spectrum
            Manually specify spectrum to get a desired wavelength weighting. 
            Only applicable if ``quick=False``. If not set, defaults to ``self.sp``.
        """

        # Work with oversampled pixels and downsample at end
        siaf_ap = self.inst_on.siaf_ap
        osamp = self.inst_on.oversample
        ny = nx = self.fov_pix * osamp

        # Locations in aperture ideal frame to produce PSFs
        if mode == 'rth':
            r, th = loc
            xidl, yidl = self.rth_to_xy(r, th, PA_V3=PA_V3, frame_out='idl', addV3Yidl=addV3Yidl)
        elif mode == 'xy':
            xidl, yidl = loc

        # Perform shift in idl frame then rotate to sky coords
        psf = self.gen_psf_idl((xidl, yidl), coord_frame='idl', do_shift=do_shift, 
                                return_oversample=True, **kwargs)

        if do_shift:
            # Shifting PSF, means rotate such that North is up
            psf = psf.reshape([-1,ny,nx])
            # Get aperture position angle
            PA_ap = PA_V3 + siaf_ap.V3IdlYAngle
            psf = rotate(psf, -PA_ap, reshape=False, mode='constant', cval=0, axes=(-1,-2))

        # Resample to detector pixels?
        if not return_oversample:
            psf = frebin(psf, scale=1/osamp)

        # Normalize to 1
        psf = psf.squeeze()
        if normalize == True:
            psf = psf / np.sum(psf)

        return psf