
from tqdm import tqdm

import radio_beam
from reproject import reproject_interp
from spectral_cube import SpectralCube, Projection
from spectral_cube import wcs_utils
from astropy.io import fits
from astropy import units as u
from astropy import log
import numpy as np
from astropy import wcs
from astropy import stats
from astropy.convolution import convolve_fft, Gaussian2DKernel
from spectral_cube.dask_spectral_cube import DaskSpectralCube, DaskVaryingResolutionSpectralCube


def feather_kernel(nax2, nax1, lowresfwhm, pixscale):
    """
    Construct the weight kernels (image arrays) for the fourier transformed low
    resolution and high resolution images.  The kernels are the fourier transforms
    of the low-resolution beam and (1-[that kernel])

    Parameters
    ----------
    nax2, nax1 : int
       Number of pixels in each axes.
    lowresfwhm : float
       Angular resolution of the low resolution image (FWHM)
    pixscale : quantity (arcsec equivalent)
       pixel size in the input high resolution image.

    Return
    ----------
    kfft : float array
       An image array containing the weighting for the low resolution image
    ikfft : float array
       An image array containing the weighting for the high resolution image
       (simply 1-kfft)
    """
    # Construct arrays which hold the x and y coordinates (in unit of pixels)
    # of the image
    ygrid,xgrid = (np.indices([nax2,nax1]) -
                   np.array([(nax2-1.)/2,(nax1-1.)/2.])[:,None,None])

    # constant converting "resolution" in fwhm to sigma
    fwhm = np.sqrt(8*np.log(2))

    if not hasattr(pixscale, 'unit'):
        pixscale = u.Quantity(pixscale, u.deg)

    # sigma in pixels
    sigma = ((lowresfwhm/fwhm/(pixscale)).decompose().value)
    # log.info(f"sigma: {sigma}, lowresfwhm: {lowresfwhm}, pixscale: {pixscale}")

    # not used, just noted that these are the theoretical values (...maybe...)
    #sigma_fftspace = (1/(4*np.pi**2*sigma**2))**0.5
    #sigma_fftspace = (2*np.pi*sigma)**-1
    #log.debug('sigma = {0}, sigma_fftspace={1}'.format(sigma, sigma_fftspace))

    # technically, the fftshift here does nothing since we're using the
    # absolute value of the kernel below, so the phase is irrelevant
    kernel = np.fft.fftshift(np.exp(-(xgrid**2+ygrid**2)/(2*sigma**2)))
    # convert the kernel, which is just a gaussian in image space,
    # to its corresponding kernel in fourier space
    kfft = np.abs(np.fft.fft2(kernel)) # should be mostly real

    if np.any(np.isnan(kfft)):
        raise ValueError("NaN value encountered in kernel")

    # normalize the kernel
    kfft/=kfft.max()
    ikfft = 1-kfft

    return kfft, ikfft


def fftmerge(kfft, ikfft, im_hi, im_lo,  lowpassfilterSD=False,
             replace_hires=False, deconvSD=False, min_beam_fraction=0.1):
    """
    Combine images in the fourier domain, and then output the combined image
    both in fourier domain and the image domain.

    Parameters
    ----------
    kernel1,2 : float array
       Weighting images.
    im1,im2: float array
       Input images.
    lowpassfilterSD: bool or str
        Re-convolve the SD image with the beam?  If ``True``, the SD image will
        be weighted by the deconvolved beam.
    replace_hires: Quantity or False
        If set, will simply replace the fourier transform of the single-dish
        data with the fourier transform of the interferometric data above the
        specified kernel level.  Can be used in conjunction with either
        ``lowpassfilterSD`` or ``deconvSD``.  Must be set to a floating-point
        threshold value; this threshold will be applied to the single-dish
        kernel.
    deconvSD: bool
        Deconvolve the single-dish data before adding in fourier space?
        This "deconvolution" is a simple division of the fourier transform
        of the single dish image by its fourier transformed beam
    min_beam_fraction : float
        The minimum fraction of the beam to include; values below this fraction
        will be discarded when deconvolving

    Returns
    -------
    fftsum : float array
       Combined image in fourier domain.
    combo  : float array
       Combined image in image domain.
    """

    fft_hi = np.fft.fft2(np.nan_to_num(im_hi))
    fft_lo = np.fft.fft2(np.nan_to_num(im_lo))

    # Combine and inverse fourier transform the images
    if lowpassfilterSD:
        lo_conv = kfft*fft_lo
    elif deconvSD:
        lo_conv = fft_lo / kfft
        lo_conv[kfft < min_beam_fraction] = 0
    else:
        lo_conv = fft_lo


    if replace_hires:
        if replace_hires is True:
            raise ValueError("If you are specifying replace_hires, "
                             "you must give a floating point value "
                             "corresponding to the beam-fraction of the "
                             "single-dish image below which the "
                             "high-resolution data will be used.")
        fftsum = lo_conv.copy()

        # mask where the hires data is above a threshold
        mask = ikfft > replace_hires

        fftsum[mask] = fft_hi[mask]
    else:
        fftsum = lo_conv + ikfft*fft_hi

    combo = np.fft.ifft2(fftsum)

    return fftsum, combo


def simple_deconvolve_sdim(hdu, lowresfwhm, minval=1e-1):
    """
    Perform a very simple fourier-space deconvolution of single-dish data.
    i.e., divide the fourier transform of the single-dish data by the fourier
    transform of its beam, and fourier transform back.

    This is not a generally useful method!
    """
    proj = Projection.from_hdu(hdu)

    nax2, nax1 = proj.shape
    pixscale = wcs.utils.proj_plane_pixel_area(proj.wcs.celestial)**0.5

    kfft, ikfft = feather_kernel(nax2, nax1, lowresfwhm, pixscale)

    fft_lo = (np.fft.fft2(np.nan_to_num(proj.value)))

    # Divide by the SD beam in Fourier space.
    decfft_lo = fft_lo.copy()
    decfft_lo[kfft > minval] = (fft_lo / kfft)[kfft > minval]
    dec_lo = np.fft.ifft2(decfft_lo)

    return dec_lo


def simple_fourier_unsharpmask(hdu, lowresfwhm, minval=1e-1):
    """
    Like simple_deconvolve_sdim, try unsharp masking by convolving
    with (1-kfft) in the fourier domain
    """
    proj = Projection.from_hdu(hdu)

    nax2, nax1 = proj.shape
    pixscale = wcs.utils.proj_plane_pixel_area(proj.wcs.celestial)**0.5

    kfft, ikfft = feather_kernel(nax2, nax1, lowresfwhm, pixscale)

    fft_hi = (np.fft.fft2(np.nan_to_num(proj.value)))
    #umaskfft_hi = fft_hi.copy()
    #umaskfft_hi[ikfft < minval] = (fft_hi * ikfft)[ikfft < minval]
    umaskfft_hi = fft_hi * ikfft
    umask_hi = np.fft.ifft2(umaskfft_hi)

    return umask_hi

def feather_simple(hires, lores,
                   highresextnum=0,
                   lowresextnum=0,
                   highresscalefactor=1.0,
                   lowresscalefactor=1.0,
                   pbresponse=None,
                   lowresfwhm=None,
                   lowpassfilterSD=False,
                   replace_hires=False,
                   deconvSD=False,
                   return_hdu=False,
                   return_regridded_lores=False,
                   match_units=True,
                   weights=None,
                   ):
    """
    Fourier combine two single-plane images.  This follows the CASA approach,
    as far as it is discernable.  Both images should be actual images of the
    sky in the same units, not fourier models or deconvolved images.

    The default parameters follow Equation 11 in section 5.2 of
    http://esoads.eso.org/abs/2002ASPC..278..375S
    very closely, except the interferometric data are not re-convolved with the
    interferometric beam.  It seems that S5.2 of Stanimirovic et al 2002
    actually wants the deconvolved *model* data, not the deconvolved *clean*
    data, as input, which would explain their equation.  This may be a
    different use of the same words.  ``lowresscalefactor`` corresponds to
    their ``f`` parameter.

    There is a remaining question: does CASA-feather re-weight the SD image by
    its beam, as suggested in
    https://science.nrao.edu/science/meetings/2016/vla-data-reduction/DR2016imaging_jott.pdf
    page 24, or does it leave the single-dish image unweighted (assuming its
    beam size is the same as the effective beam size of the image) as inferred
    from the Feather source code?

    Parameters
    ----------
    highresfitsfile : str
        The high-resolution FITS file
    lowresfitsfile : str
        The low-resolution (single-dish) FITS file
    highresextnum : int
        The extension number to use from the high-res FITS file
    lowresextnum : int
        The extension number to use from the low-res FITS file
    highresscalefactor : float
        A factor to multiply the high-resolution data by to match the
        low- or high-resolution data
    lowresscalefactor : float
        A factor to multiply the low-resolution data by to match the
        low- or high-resolution data
    pbresponse : `~numpy.ndarray`
        The primary beam response of the high-resolution data. When given,
        `highresfitsfile` should **not** be primary-beam corrected.
        `pbresponse` will be multiplied with `lowresfitsfile`, and the
        feathered image will be divided by `pbresponse` to create the final
        image.
    lowresfwhm : `astropy.units.Quantity`
        The full-width-half-max of the single-dish (low-resolution) beam;
        or the scale at which you want to try to match the low/high resolution
        data
    lowpassfilterSD: bool or str
        Re-convolve the SD image with the beam?  If ``True``, the SD image will
        be weighted by the beam, which effectively means it will be convolved
        with the beam before merging with the interferometer data.  This isn't
        really the right behavior; it should be filtered with the *deconvolved*
        beam, but in the current framework that effectively means "don't weight
        the single dish data".  See
        http://keflavich.github.io/blog/what-does-feather-do.html
        for further details about what feather does and how this relates.
    replace_hires: Quantity or False
        If set, will simply replace the fourier transform of the single-dish
        data with the fourier transform of the interferometric data above the
        specified kernel level.  Can be used in conjunction with either
        ``lowpassfilterSD`` or ``deconvSD``.  Must be set to a floating-point
        threshold value; this threshold will be applied to the single-dish
        kernel.
    deconvSD: bool
        Deconvolve the single-dish data before adding in fourier space?
        This "deconvolution" is a simple division of the fourier transform
        of the single dish image by its fourier transformed beam
    return_hdu : bool
        Return an HDU instead of just an image.  It will contain two image
        planes, one for the real and one for the imaginary data.
    return_regridded_lores : bool
        Return the 2nd image regridded into the pixel space of the first?
    match_units : bool
        Attempt to match the flux units between the files before combining?
        See `match_flux_units`.
    weights : `~numpy.ndarray`, optional
        Provide an array of weights with the spatial shape of the high-res
        data. This is useful when either of the data have emission at the map
        edge, which will lead to ringing in the Fourier transform. A weights
        array can be provided to smoothly taper the edges of each map to avoid
        this issue. **This will be applied to both the low and high resolution
        images!**

    Returns
    -------
    combo : image
        The image of the combined low and high resolution data sets
    combo_hdu : fits.PrimaryHDU
        (optional) the image encased in a FITS HDU with the relevant header
    """

    if isinstance(hires, str):
        hdu_hi = fits.open(hires)[highresextnum]
        proj_hi = Projection.from_hdu(hdu_hi)
    elif isinstance(hires, fits.PrimaryHDU):
        proj_hi = Projection.from_hdu(hires)
    else:
        proj_hi = hires

    if isinstance(lores, str):
        hdu_lo = fits.open(lores)[lowresextnum]
        proj_lo = Projection.from_hdu(hdu_lo)
    elif isinstance(lores, fits.PrimaryHDU):
        proj_lo = Projection.from_hdu(lores)
    else:
        proj_lo = lores

    if lowresfwhm is None:
        beam_low = proj_lo.beam
        lowresfwhm = beam_low.major
        # log.info("Low-res FWHM: {0}".format(lowresfwhm))

    # If weights are given, they must match the shape of the hires data
    if weights is not None:
        if not weights.shape == proj_hi.shape:
            raise ValueError("weights must be an array with the same shape as"
                             " the high-res data.")
    else:
        weights = 1.

    if pbresponse is not None:
        if not pbresponse.shape == proj_hi.shape:
            raise ValueError("pbresponse must be an array with the same"
                             " shape as the high-res data.")

    if match_units:
        # After this step, the units of im_hi are some sort of surface brightness
        # unit equivalent to that specified in the high-resolution header's units
        # Note that this step does NOT preserve the values of im_lowraw and
        # header_lowraw from above

        proj_lo = proj_lo.to(proj_hi.unit)

        # When in a per-beam unit, we need to scale the low res to the
        # Jy / beam for the HIRES beam.
        jybm_unit = u.Jy / u.beam
        if proj_hi.unit.is_equivalent(jybm_unit):
            proj_lo *= (proj_hi.beam.sr / proj_lo.beam.sr).decompose().value

    # Add check that the units are compatible
    equiv_units = proj_lo.unit.is_equivalent(proj_hi.unit)
    if not equiv_units:
        raise ValueError("Brightness units are not equivalent: "
                         f"hires: {proj_hi.unit}; lowres: {proj_lo.unit}")

    is_wcs_eq = proj_lo.wcs.wcs.compare(proj_lo.wcs.wcs)
    is_eq_shape = proj_lo.shape == proj_hi.shape

    if not is_wcs_eq or not is_eq_shape:
        proj_lo_regrid = proj_lo.reproject(proj_hi.header)
    else:
        proj_lo_regrid = proj_lo

    # Apply the pbresponse to the regridded low-resolution data
    if pbresponse is not None:
        proj_lo_regrid *= pbresponse

    pixscale = wcs.utils.proj_plane_pixel_scales(proj_hi.wcs.celestial)[0]
    nax2, nax1 = proj_hi.shape
    kfft, ikfft = feather_kernel(nax2, nax1, lowresfwhm, pixscale,)

    fftsum, combo = fftmerge(kfft, ikfft,
                             proj_hi.value * highresscalefactor * weights,
                             proj_lo_regrid.value * lowresscalefactor * weights,
                             replace_hires=replace_hires,
                             lowpassfilterSD=lowpassfilterSD,
                             deconvSD=deconvSD,
                             )

    # Divide by the PB response
    if pbresponse is not None:
        combo /= pbresponse

    if return_hdu:
        combo_hdu = fits.PrimaryHDU(data=combo.real, header=proj_hi.header)
        combo = combo_hdu

    if return_regridded_lores:
        return combo, proj_lo
    else:
        return combo


def feather_plot(hires, lores,
                 highresextnum=0,
                 lowresextnum=0,
                 highresscalefactor=1.0,
                 lowresscalefactor=1.0,
                 lowresfwhm=None,
                 lowpassfilterSD=False,
                 xaxisunit='arcsec',
                 hires_threshold=None,
                 lores_threshold=None,
                 match_units=True,
                ):
    """
    Plot the power spectra of two images that would be combined
    along with their weights.

    High-res will be shown in red, low-res in blue.

    Parameters
    ----------
    highresfitsfile : str
        The high-resolution FITS file
    lowresfitsfile : str
        The low-resolution (single-dish) FITS file
    highresextnum : int
        The extension number to use from the high-res FITS file
    highresscalefactor : float
    lowresscalefactor : float
        A factor to multiply the high- or low-resolution data by to match the
        low- or high-resolution data
    lowresfwhm : `astropy.units.Quantity`
        The full-width-half-max of the single-dish (low-resolution) beam;
        or the scale at which you want to try to match the low/high resolution
        data
    xaxisunit : 'arcsec' or 'lambda'
        The X-axis units.  Either arcseconds (angular scale on the sky)
        or baseline length (lambda)
    hires_threshold : float or None
    lores_threshold : float or None
        Threshold to cut off before computing power spectrum to remove
        the noise contribution.  Threshold will be applied *after* scalefactor.
    match_units : bool
        Attempt to match the flux units between the files before combining?
        See `match_flux_units`.

    Returns
    -------
    combo : image
        The image of the combined low and high resolution data sets
    combo_hdu : fits.PrimaryHDU
        (optional) the image encased in a FITS HDU with the relevant header
    """
    # import image_tools
    from turbustat.statistics.psds import pspec

    if isinstance(hires, str):
        hdu_hi = fits.open(hires)[highresextnum]
        proj_hi = Projection.from_hdu(hdu_hi)
    elif isinstance(hires, fits.PrimaryHDU):
        proj_hi = Projection.from_hdu(hires)
    else:
        proj_hi = hires

    if isinstance(lores, str):
        hdu_lo = fits.open(lores)[lowresextnum]
        proj_lo = Projection.from_hdu(hdu_lo)
    elif isinstance(lores, fits.PrimaryHDU):
        proj_lo = Projection.from_hdu(lores)
    else:
        proj_lo = lores

    print("featherplot")
    pb = tqdm(13)

    if match_units:
        # After this step, the units of im_hi are some sort of surface brightness
        # unit equivalent to that specified in the high-resolution header's units
        # Note that this step does NOT preserve the values of im_lowraw and
        # header_lowraw from above
        proj_lo = proj_lo.to(proj_hi.unit)

    # Add check that the units are compatible
    equiv_units = proj_lo.unit.is_equivalent(proj_hi.unit)
    if not equiv_units:
        raise ValueError("Brightness units are not equivalent: "
                         f"hires: {proj_hi.unit}; lowres: {proj_lo.unit}")

    proj_lo_regrid = proj_lo.reproject(proj_hi.header)

    pb.update()

    if lowresfwhm is None:
        beam_low = proj_lo.beam
        lowresfwhm = beam_low.major
        log.info("Low-res FWHM: {0}".format(lowresfwhm))

    pixscale = wcs.utils.proj_plane_pixel_scales(proj_hi.wcs.celestial)[0]

    nax2, nax1 = proj_hi.shape
    kfft, ikfft = feather_kernel(nax2, nax1, lowresfwhm, pixscale)

    log.debug("bottom-left pixel before shifting: kfft={0}, ikfft={1}".format(kfft[0,0], ikfft[0,0]))
    print("bottom-left pixel before shifting: kfft={0}, ikfft={1}".format(kfft[0,0], ikfft[0,0]))
    pb.update()
    kfft = np.fft.fftshift(kfft)
    pb.update()
    ikfft = np.fft.fftshift(ikfft)
    pb.update()

    if hires_threshold is None:
        fft_hi = np.fft.fftshift(np.fft.fft2(np.nan_to_num(proj_hi.value * highresscalefactor)))
    else:
        hires_tofft = np.nan_to_num(proj_hi.value * highresscalefactor)
        hires_tofft[hires_tofft < hires_threshold] = 0
        fft_hi = np.fft.fftshift(np.fft.fft2(hires_tofft))
    pb.update()
    if lores_threshold is None:
        fft_lo = np.fft.fftshift(np.fft.fft2(np.nan_to_num(proj_lo_regrid.value * lowresscalefactor)))
    else:
        lores_tofft = np.nan_to_num(proj_lo_regrid.value * lowresscalefactor)
        lores_tofft[lores_tofft < lores_threshold] = 0
        fft_lo = np.fft.fftshift(np.fft.fft2(lores_tofft))
    pb.update()

    # rad,azavg_kernel = image_tools.radialprofile.azimuthalAverage(np.abs(kfft), returnradii=True)
    # pb.update()
    # rad,azavg_ikernel = image_tools.radialprofile.azimuthalAverage(np.abs(ikfft), returnradii=True)
    # pb.update()
    # rad,azavg_hi = image_tools.radialprofile.azimuthalAverage(np.abs(fft_hi), returnradii=True)
    # pb.update()
    # rad,azavg_lo = image_tools.radialprofile.azimuthalAverage(np.abs(fft_lo), returnradii=True)
    # pb.update()
    # rad,azavg_hi_scaled = image_tools.radialprofile.azimuthalAverage(np.abs(fft_hi*ikfft), returnradii=True)
    # pb.update()
    # rad,azavg_lo_scaled = image_tools.radialprofile.azimuthalAverage(np.abs(fft_lo*kfft), returnradii=True)
    # pb.update()
    # rad,azavg_lo_deconv = image_tools.radialprofile.azimuthalAverage(np.abs(fft_lo/kfft), returnradii=True)
    # pb.update()

    rad,azavg_kernel = pspec(np.abs(kfft))
    pb.update()
    rad,azavg_ikernel = pspec(np.abs(ikfft))
    pb.update()
    rad,azavg_hi = pspec(np.abs(fft_hi))
    pb.update()
    rad,azavg_lo = pspec(np.abs(fft_lo))
    pb.update()
    rad,azavg_hi_scaled = pspec(np.abs(fft_hi*ikfft))
    pb.update()
    rad,azavg_lo_scaled = pspec(np.abs(fft_lo*kfft))
    pb.update()
    rad,azavg_lo_deconv = pspec(np.abs(fft_lo/kfft))
    pb.update()

    # use the same "OK" mask for everything because it should just be an artifact
    # of the averaging
    OK = np.isfinite(azavg_kernel)

    # 1/min(rad) ~ number of pixels from center to corner of image
    # pixscale in degrees.  Convert # pixels to arcseconds
    # 2 pixels = where rad is 1/2 the image (square) dimensions
    # (nax1) pixels = where rad is 1
    # *** ASSUMES SQUARE ***
    rad_pix = nax1/rad
    rad_as = pixscale * rad_pix
    log.debug("pixscale={0} nax1={1}".format(pixscale, nax1))
    if xaxisunit == 'lambda':
        #restfrq = (wcs.WCS(hd1).wcs.restfrq*u.Hz)
        lam = 1./(rad_as*u.arcsec).to(u.rad).value
        xaxis = lam
    elif xaxisunit == 'arcsec':
        xaxis = rad_as
    else:
        raise ValueError("xaxisunit must be in (arcsec, lambda)")


    import matplotlib.pyplot as pl

    pl.clf()
    ax1 = pl.subplot(2,1,1)
    ax1.loglog(xaxis[OK], azavg_kernel[OK], color='b', linewidth=2, alpha=0.8,
               label="Low-res Kernel")
    ax1.loglog(xaxis[OK], azavg_ikernel[OK], color='r', linewidth=2, alpha=0.8,
               label="High-res Kernel")
    ax1.vlines(lowresfwhm.to(u.arcsec).value, 1e-5, 1.1, linestyle='--', color='k')
    ax1.set_ylim(1e-5, 1.1)

    arg_xmin = np.nanargmin(np.abs((azavg_ikernel)-(1-1e-5)))
    xlim = xaxis[arg_xmin].value / 1.1, xaxis[1].value * 1.1
    log.debug("Xlim: {0}".format(xlim))
    assert np.isfinite(xlim[0])
    assert np.isfinite(xlim[1])
    ax1.set_xlim(*xlim)

    ax1.set_ylabel("Kernel Weight")

    ax1.legend()

    ax2 = pl.subplot(2,1,2)
    ax2.loglog(xaxis[OK], azavg_lo[OK], color='b', linewidth=2, alpha=0.8,
               label="Low-res image")
    ax2.loglog(xaxis[OK], azavg_hi[OK], color='r', linewidth=2, alpha=0.8,
               label="High-res image")
    ax2.set_xlim(*xlim)
    ax2.set_ylim(min([azavg_lo[arg_xmin], azavg_lo[2], azavg_hi[arg_xmin], azavg_hi[2]]),
                 1.1*max([np.nanmax(azavg_lo), np.nanmax(azavg_hi)]),
                )
    ax2.set_ylabel("Power spectrum $|FT|$")

    ax2.loglog(xaxis[OK], azavg_lo_scaled[OK], color='b', linewidth=2, alpha=0.5,
               linestyle='--',
               label="Low-res scaled image")
    ax2.loglog(xaxis[OK], azavg_lo_deconv[OK], color='b', linewidth=2, alpha=0.5,
               linestyle=':',
               label="Low-res deconvolved image")
    ax2.loglog(xaxis[OK], azavg_hi_scaled[OK], color='r', linewidth=2, alpha=0.5,
               linestyle='--',
               label="High-res scaled image")
    ax2.set_xlim(*xlim)
    if xaxisunit == 'arcsec':
        ax2.set_xlabel("Angular Scale (arcsec)")
    elif xaxisunit == 'lambda':
        ax2.set_xscale('linear')
        ax2.set_xlim(0, xlim[0])
        ax2.set_xlabel("Baseline Length (lambda)")
    ax2.set_ylim(min([azavg_lo_scaled[arg_xmin], azavg_lo_scaled[2], azavg_hi_scaled[arg_xmin], azavg_hi_scaled[2]]),
                 1.1*max([np.nanmax(azavg_lo_scaled), np.nanmax(azavg_hi_scaled)]),
                )

    ax2.legend()

    return {'radius':rad,
            'radius_as': rad_as,
            'azimuthally_averaged_kernel': azavg_kernel,
            'azimuthally_averaged_inverse_kernel': azavg_ikernel,
            'azimuthally_averaged_low_resolution': azavg_lo,
            'azimuthally_averaged_high_resolution': azavg_hi,
            'azimuthally_averaged_low_res_filtered': azavg_lo_scaled,
            'azimuthally_averaged_high_res_filtered': azavg_hi_scaled,
           }

try:
    import dask
    HAS_DASK = True
except ImportError:
    HAS_DASK = False

if HAS_DASK:
    import dask.array as da
    from spectral_cube.dask_spectral_cube import add_save_to_tmp_dir_option

    @add_save_to_tmp_dir_option
    def _dask_feather_cubes(cube_hi, cube_lo,
                            highresscalefactor=1.0,
                            lowresscalefactor=1.0,
                            weights=1.0,
                            replace_hires=False,
                            lowpassfilterSD=False,
                            deconvSD=False):

        lowresfwhm = cube_lo.beam.major

        pixscale = wcs.utils.proj_plane_pixel_scales(cube_hi.wcs.celestial)[0]
        nax2, nax1 = cube_hi.shape[1:]

        # Do we need this wrapper here?
        def feather_wrapper(img_hi, img_lo, **kwargs):

            kfft, ikfft = feather_kernel(nax2, nax1, lowresfwhm, pixscale,)

            fftsum, combo = fftmerge(kfft, ikfft,
                                    img_hi * highresscalefactor * weights,
                                    img_lo * lowresscalefactor * weights,
                                    replace_hires=replace_hires,
                                    lowpassfilterSD=lowpassfilterSD,
                                    deconvSD=deconvSD,
                                    )

            return combo.real

        data_lo = cube_lo._get_filled_data(fill=np.nan)

        feath_cube = cube_hi._map_blocks_to_cube(feather_wrapper,
                                                 additional_arrays=[data_lo])

        return feath_cube




def feather_simple_cube(cube_hi, cube_lo,
                        allow_spectral_resample=True,
                        allow_huge_operations=False,
                        use_memmap=True,
                        use_dask=False,
                        use_save_to_tmp_dir=False,
                        force_spatial_rechunk=True,
                        channels_per_chunk='auto',
                        allow_lo_reproj=True,
                        **kwargs):
    """
    Parameters
    ----------
    cube_hi : '~spectral_cube.SpectralCube' or str
        The high-resolution spectral-cube or name of FITS file.
    cube_lo : '~spectral_cube.SpectralCube' or str
        The low-resolution spectral-cube or name of FITS file.
    allow_spectral_resample : bool
        If True, will run `~SpectralCube.spectral_interpolate` to match the spectral axes
        of the data. Note that spectral smoothing may need to be first applied when downsampling
        along the spectral axis; this should be applied to the input data prior to feathering.
        If False, a ValueError is raised when the spectral axes of the cubes differ.
    allow_huge_operations : bool
        Sets `~spectral_cube.SpectralCube.allow_huge_operations`. If True, no memory related
        errors will be raise prior to computing. If False, an error will be raise if the cube
        size is too large (currently set to ~1 GB in spectral-cube).
    use_memmap : bool
        Enable saving the feathered cube to a memory-mapped array to avoid
        having the whole output cube in memory.
    use_dask : bool
        Enable feathering using dask operations. See the `spectral-cube documentation <https://spectral-cube.readthedocs.io/en/latest/dask.html>`_
        for more information on using dask with spectral-cube.
    use_save_to_tmp_dir : bool
        With `use_dask` enabled, when `True` will save intermediate operations
        to a temporary zarr file. This forces dask to perform each computation and
        can be useful for operations that are optimized with different rechunking
        schemes.
    force_spatial_rechunk : bool
        With `use_dask` enabled, `True` forces rechunking both cubes to
        ensure the chunk sizes match and have contiguous spatial chunks
        (i.e., chunk only along the spectral axis).
    channels_per_chunk : str or int
        With `use_dask` enabled, allows setting the number of channels in each chunk.
        The default is 'auto', allowing dask to choose the optimal chunk size. `-1` will
        force the entire cube into a single chunk and may cause memory issues.
    allow_lo_reproj : bool
        With `use_dask` enabled, `cube_lo` will be reprojected to match
        `cube_hi`. This step can otherwise be performed prior to feathering
        but is needed to force alignment of the chunks in both cubes.
    kwargs : Passed to `~feather_simple`.

    Returns
    -------
    feathcube : '~spectral_cube.SpectralCube'
        The combined feathered spectral cube.

    """

    if not hasattr(cube_hi, 'shape'):
        cube_hi = SpectralCube.read(cube_hi, use_dask=use_dask)
    if not hasattr(cube_lo, 'shape'):
        cube_lo = SpectralCube.read(cube_lo, use_dask=use_dask)

    # TODO: add VRSC dask suppoert
    # Cannot handle varying res with dask yet
    if isinstance(cube_lo, DaskVaryingResolutionSpectralCube):
        raise TypeError("`feather_simple_cube` cannot yet handle varying resolution spectral cubes"
                        " (beam size per channel). Use a non-dask for now.")

    if isinstance(cube_lo, DaskSpectralCube):
        save_kwargs = {"save_to_tmp_dir": use_save_to_tmp_dir}
    else:
        save_kwargs = {}

    cube_hi.allow_huge_operations = allow_huge_operations
    cube_lo.allow_huge_operations = allow_huge_operations

    if cube_lo.shape[0] == cube_hi.shape[0]:
        is_spec_matched = np.isclose(cube_lo.spectral_axis, cube_hi.spectral_axis).all()
    else:
        is_spec_matched = False

    if not is_spec_matched:
        if allow_spectral_resample:
            cube_lo = cube_lo.spectral_interpolate(cube_hi.spectral_axis, **save_kwargs)
        else:
            raise ValueError("Spectral axes do not match. Enable `allow_spectrum_resample` to "
                             "spectrally match the low resolution to high resolution data.")

    # If cubes are DaskSpectralCubes, use the dask implementation
    if isinstance(cube_hi, DaskSpectralCube) and isinstance(cube_lo, DaskSpectralCube):

        # The block mapping has to be the same. Set here whether to
        # allow a prior reproject operation for the SD to match.
        if allow_lo_reproj:
            # Add a check to see if we can avoid reprojecting as it's expensive
            # for whole cubes.
            is_wcs_eq = cube_hi.wcs.celestial.wcs.compare(cube_lo.wcs.celestial.wcs)
            is_eq_shape = cube_hi.shape == cube_lo.shape

            if is_wcs_eq and is_eq_shape:
                cube_lo_reproj = cube_lo
            else:
                # NOTE: is this memory friendly? We don't have a dedicated
                # dask reprojection task, so this COULD break things.
                cube_lo = cube_lo.rechunk((channels_per_chunk, -1, -1), **save_kwargs)
                cube_lo_reproj = cube_lo.reproject(cube_hi.header, use_memmap=use_memmap)
        else:
            cube_lo_reproj = cube_lo

        # Check that the pixel sizes of both cubes now match
        equal_sizes = cube_lo_reproj.shape == cube_hi.shape
        if not equal_sizes:
            raise ValueError("The cube_lo array shape does not match the cube_hi"
                             " shape. Enable `allow_lo_reproj` or reproject cube_lo"
                             " before feathering.")

        # Ensure spatial chunk sizes are matched.
        if force_spatial_rechunk:
            chunksize = (channels_per_chunk, -1, -1)
            cube_hi = cube_hi.rechunk(chunksize, **save_kwargs)
            cube_lo_reproj = cube_lo_reproj.rechunk(chunksize, **save_kwargs)

            if cube_hi._data.chunksize != cube_lo_reproj._data.chunksize:
                raise ValueError("The chunk size does not match between the cubes."
                                 f" cube_hi: {cube_hi._data.chunksize} "
                                 f" cube_lo_reproj: {cube_lo_reproj._data.chunksize} "
                                 "Check reprojection or apply prior to feathering.")

        # Check that we have a single chunk size in the spatial dimensions.
        # This is required for the fft per plane for feathering.
        has_one_spatial_chunk_hi = cube_hi.shape[1:] == cube_hi._data.chunksize[1:]
        has_one_spatial_chunk_lo = cube_lo_reproj.shape[1:] == cube_lo_reproj._data.chunksize[1:]

        if not has_one_spatial_chunk_hi or not has_one_spatial_chunk_lo:
            raise ValueError("Cubes must have a single chunk along the spatial axes."
                             f" cube_lo has chunksize: {cube_lo_reproj._data.chunksize}."
                             f" cube_hi has chunksize: {cube_hi._data.chunksize}."
                             " Enable `force_spatial_rechunk=True` to rechunk the cubes.")

        # Check kwargs for feather_simple kwarg to allow matching units
        match_units = kwargs.pop('match_units', True)

        # Check for units consistency
        if match_units:
            # After this step, the units of cube_hi are some sort of surface brightness
            # unit equivalent to that specified in the high-resolution header's units

            # NOTE: is this memory-friendly??
            cube_lo_reproj = cube_lo_reproj.to(cube_hi.unit)

            # When in a per-beam unit, we need to scale the low res to the
            # Jy / beam for the HIRES beam.
            jybm_unit = u.Jy / u.beam
            if cube_hi.unit.is_equivalent(jybm_unit):
                cube_lo_reproj *= (cube_hi.beam.sr / cube_lo_reproj.beam.sr).decompose().value

        # Add check that the units are compatible
        equiv_units = cube_lo_reproj.unit.is_equivalent(cube_hi.unit)
        if not equiv_units:
            raise ValueError("Brightness units are not equivalent: "
                            f"hires: {cube_hi.unit}; lowres: {cube_lo_reproj.unit}")

        feathcube = _dask_feather_cubes(cube_hi, cube_lo_reproj,
                                        save_to_tmp_dir=use_save_to_tmp_dir,
                                        **kwargs)

    else:

        if use_memmap:
            from tempfile import NamedTemporaryFile
            fname = NamedTemporaryFile()
            feath_array = np.memmap(fname, shape=cube_hi.shape, dtype=float, mode='w+')
        else:
            feath_array = np.empty(cube_hi.shape)

        pb = tqdm(cube_hi.shape[0])
        for ii in range(cube_hi.shape[0]):

            hslc = cube_hi[ii]
            lslc = cube_lo[ii]

            feath_array[ii] = feather_simple(hslc, lslc, **kwargs).real

            pb.update()

            if use_memmap:
                feath_array.flush()

        feathcube = SpectralCube(data=feath_array,
                                header=cube_hi.header,
                                wcs=cube_hi.wcs,
                                meta=cube_hi.meta)

    return feathcube


def feather_compare(hires, lores,
                    SAS,
                    LAS,
                    lowresfwhm,
                    highresextnum=0,
                    lowresextnum=0,
                    beam_divide_lores=True,
                    lowpassfilterSD=False,
                    min_beam_fraction=0.1,
                    plot_min_beam_fraction=1e-3,
                    doplot=True,
                    return_samples=False,
                    weights=None,
                   ):
    """
    Compare the single-dish and interferometer data over the region where they
    should agree

    Parameters
    ----------
    highresfitsfile : str
        The high-resolution FITS file
    lowresfitsfile : str
        The low-resolution (single-dish) FITS file
    SAS : `astropy.units.Quantity`
        The smallest angular scale to plot
    LAS : `astropy.units.Quantity`
        The largest angular scale to plot (probably the LAS of the high
        resolution data)
    lowresfwhm : `astropy.units.Quantity`
        The full-width-half-max of the single-dish (low-resolution) beam;
        or the scale at which you want to try to match the low/high resolution
        data
    highresextnum : int, optional
        Select the HDU when passing a multi-HDU FITS file for the high-resolution
        data.
    lowresextnum : int, optional
        Select the HDU when passing a multi-HDU FITS file for the low-resolution
        data.
    beam_divide_lores: bool
        Divide the low-resolution data by the beam weight before plotting?
        (should do this: otherwise, you are plotting beam-downweighted data)
    min_beam_fraction : float
        The minimum fraction of the beam to include; values below this fraction
        will be discarded when deconvolving
    plot_min_beam_fraction : float
        Like min_beam_fraction, but used only for plotting
    doplot : bool
        If true, make plots.  Otherwise will just return the results.
    return_samples : bool, optional
        Return the samples in the overlap region. This includes: the angular
        scale at each point, the ratio, the high-res values, and the low-res
        values.
    weights : `~numpy.ndarray`, optional
        Provide an array of weights with the spatial shape of the high-res
        data. This is useful when either of the data have emission at the map
        edge, which will lead to ringing in the Fourier transform. A weights
        array can be provided to smoothly taper the edges of each map to avoid
        this issue.

    Returns
    -------
    stats : dict
        Statistics on the ratio of the high-resolution FFT data to the
        low-resolution FFT data over the range SAS < x < LAS.  Sigma-clipped
        stats are included.

    """
    if LAS <= SAS:
        raise ValueError("Must have LAS > SAS. Check the input parameters.")

    if not isinstance(hires, Projection):
        if isinstance(hires, str):
            hdu_hi = fits.open(hires)[highresextnum]
        else:
            hdu_hi = hires
        proj_hi = Projection.from_hdu(hdu_hi)

    else:
        proj_hi = hires

    if not isinstance(lores, Projection):
        if isinstance(lores, str):
            hdu_lo = fits.open(lores)[lowresextnum]
        else:
            hdu_lo = lores
        proj_lo = Projection.from_hdu(hdu_lo)

    else:
        proj_lo = lores

    # If weights are given, they must match the shape of the hires data
    if weights is not None:
        if not weights.shape == proj_hi.shape:
            raise ValueError("weights must be an array with the same shape as"
                             " the high-res data.")
    else:
        weights = 1.

    proj_lo_regrid = proj_lo.reproject(proj_hi.header)

    nax2, nax1 = proj_hi.shape
    pixscale = np.abs(wcs.utils.proj_plane_pixel_scales(proj_hi.wcs.celestial)[0]) * u.deg

    kfft, ikfft = feather_kernel(nax2, nax1, lowresfwhm, pixscale)
    kfft = np.fft.fftshift(kfft)
    ikfft = np.fft.fftshift(ikfft)

    yy,xx = np.indices([nax2, nax1])
    rr = ((xx-(nax1-1)/2.)**2 + (yy-(nax2-1)/2.)**2)**0.5
    angscales = nax1/rr * pixscale

    fft_hi = np.fft.fftshift(np.fft.fft2(np.nan_to_num(proj_hi * weights)))
    fft_lo = np.fft.fftshift(np.fft.fft2(np.nan_to_num(proj_lo_regrid * weights)))
    if beam_divide_lores:
        fft_lo_deconvolved = fft_lo / kfft
    else:
        fft_lo_deconvolved = fft_lo

    below_beamscale = kfft < min_beam_fraction
    below_beamscale_plotting = kfft < plot_min_beam_fraction
    fft_lo_deconvolved[below_beamscale_plotting] = np.nan

    mask = (angscales > SAS) & (angscales < LAS) & (~below_beamscale)

    if mask.sum() == 0:
        raise ValueError("No valid uv-overlap region found. Check the inputs for "
                         "SAS and LAS.")

    ratio = np.abs(fft_hi)[mask] / np.abs(fft_lo_deconvolved)[mask]
    sclip = stats.sigma_clipped_stats(ratio, sigma=3, maxiters=5)

    if doplot:
        import matplotlib.pyplot as pl
        pl.clf()
        pl.suptitle("{0} - {1}".format(SAS,LAS))
        pl.subplot(2,2,1)
        pl.plot(np.abs(fft_hi)[mask], np.abs(fft_lo_deconvolved)[mask], '.')
        mm = np.array([np.abs(fft_hi)[mask].min(), np.abs(fft_hi)[mask].max()])
        pl.plot(mm, mm, 'k--')
        pl.plot(mm, mm/sclip[1], 'k:')
        pl.xlabel("High-resolution")
        pl.ylabel("Low-resolution")
        pl.subplot(2,2,2)

        pl.hist(ratio[np.isfinite(ratio)], bins=30)
        pl.xlabel("High-resolution / Low-resolution")
        pl.subplot(2,1,2)
        srt = np.argsort(angscales.to(u.arcsec).value[~below_beamscale_plotting])
        pl.plot(angscales.to(u.arcsec).value[~below_beamscale_plotting][srt],
                np.nanmax(np.abs(fft_lo_deconvolved))*kfft.real[~below_beamscale_plotting][srt],
                'k-', zorder=-5)
        pl.loglog(angscales.to(u.arcsec).value, np.abs(fft_hi), 'r,', alpha=0.5, label='High-res')
        pl.loglog(angscales.to(u.arcsec).value, np.abs(fft_lo_deconvolved), 'b,', alpha=0.5, label='Lo-res')
        ylim = pl.gca().get_ylim()
        pl.vlines([SAS.to(u.arcsec).value,LAS.to(u.arcsec).value],
                  ylim[0], ylim[1], linestyle='-', color='k')
        #pl.legend(loc='best')

    if return_samples:
        return (angscales.to(u.arcsec)[mask], ratio, np.abs(fft_hi)[mask],
                np.abs(fft_lo_deconvolved)[mask])

    return {'median': np.nanmedian(ratio),
            'mean': np.nanmean(ratio),
            'std': np.nanstd(ratio),
            'mean_sc': sclip[0],
            'median_sc': sclip[1],
            'std_sc': sclip[2],
           }


def angular_range_image_comparison(hires, lores, SAS, LAS, lowresfwhm,
                                   beam_divide_lores=True,
                                   lowpassfilterSD=False,
                                   min_beam_fraction=0.1,
                                   plot_min_beam_fraction=1e-3, doplot=True,):
    """
    Compare the single-dish and interferometer data over the region where they
    should agree, but do the comparison in image space!

    Parameters
    ----------
    highresfitsfile : str
        The high-resolution FITS file
    lowresfitsfile : str
        The low-resolution (single-dish) FITS file
    SAS : `astropy.units.Quantity`
        The smallest angular scale to plot
    LAS : `astropy.units.Quantity`
        The largest angular scale to plot (probably the LAS of the high
        resolution data)
    lowresfwhm : `astropy.units.Quantity`
        The full-width-half-max of the single-dish (low-resolution) beam;
        or the scale at which you want to try to match the low/high resolution
        data
    beam_divide_lores: bool
        Divide the low-resolution data by the beam weight before plotting?
        (should do this: otherwise, you are plotting beam-downweighted data)
    min_beam_fraction : float
        The minimum fraction of the beam to include; values below this fraction
        will be discarded when deconvolving
    plot_min_beam_fraction : float
        Like min_beam_fraction, but used only for plotting
    doplot : bool
        If true, make plots.  Otherwise will just return the results.

    Returns
    -------
    scalefactor : float
        The mean ratio of the high- to the low-resolution image over the range
        of shared angular sensitivity weighted by the low-resolution image
        intensity over that range.  The weighting is necessary to avoid errors
        introduced by the fact that these images are forced to have zero means.
    """
    if LAS <= SAS:
        raise ValueError("Must have LAS > SAS. Check the input parameters.")

    if isinstance(hires, str):
        hdu_hi = fits.open(hires)[highresextnum]
    else:
        hdu_hi = hires
    proj_hi = Projection.from_hdu(hdu_hi)

    if isinstance(lores, str):
        hdu_lo = fits.open(lores)[lowresextnum]
    else:
        hdu_lo = lores
    proj_lo = Projection.from_hdu(hdu_lo)

    proj_lo_regrid = proj_lo.reproject(proj_hi.header)

    nax2, nax1 = proj_hi.shape
    pixscale = wcs.utils.proj_plane_pixel_scales(proj_hi.wcs.celestial)[0]

    kfft, ikfft = feather_kernel(nax2, nax1, lowresfwhm, pixscale)
    kfft = np.fft.fftshift(kfft)
    ikfft = np.fft.fftshift(ikfft)

    yy,xx = np.indices([nax2, nax1])
    rr = ((xx-(nax1-1)/2.)**2 + (yy-(nax2-1)/2.)**2)**0.5
    angscales = nax1/rr * pixscale*u.deg

    fft_hi = np.fft.fftshift(np.fft.fft2(np.nan_to_num(proj_hi)))
    fft_lo = np.fft.fftshift(np.fft.fft2(np.nan_to_num(proj_lo_regrid)))
    if beam_divide_lores:
        fft_lo_deconvolved = fft_lo / kfft
    else:
        fft_lo_deconvolved = fft_lo

    below_beamscale = kfft < min_beam_fraction
    below_beamscale_plotting = kfft < plot_min_beam_fraction
    fft_lo_deconvolved[below_beamscale_plotting] = np.nan

    mask = (angscales > SAS) & (angscales < LAS) & (~below_beamscale)
    if mask.sum() == 0:
        raise ValueError("No valid uv-overlap region found. Check the inputs for "
                         "SAS and LAS.")

    hi_img_ring = (np.fft.ifft2(np.fft.fftshift(fft_hi*mask)))
    lo_img_ring = (np.fft.ifft2(np.fft.fftshift(fft_lo*mask)))
    lo_img_ring_deconv = (np.fft.ifft2(np.fft.fftshift(np.nan_to_num(fft_lo_deconvolved*mask))))

    lo_img = lo_img_ring_deconv if beam_divide_lores else lo_img_ring

    ratio = (hi_img_ring.real).ravel() / (lo_img.real).ravel()
    sd_weighted_mean_ratio = (((lo_img.real.ravel())**2 * ratio).sum() /
                              ((lo_img.real.ravel())**2).sum())

    return sd_weighted_mean_ratio


def scale_comparison(original_image, test_image, scales, sm_orig=True):
    """
    Compare the 'test_image' to the original image as a function of scale (in
    pixel units)

    (warning: kinda slow)
    """

    chi2s = []

    for scale in scales:

        kernel = Gaussian2DKernel(scale)

        sm_img = convolve_fft(test_image, kernel)
        if sm_orig:
            sm_orig_img = convolve_fft(original_image, kernel)
        else:
            sm_orig_img = original_image

        chi2 = (((sm_img-sm_orig_img)/sm_orig_img)**2).sum()

        chi2s.append(chi2)

    return np.array(chi2s)
