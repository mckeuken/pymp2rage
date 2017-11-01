import nibabel as nb
from nilearn import image, masking
import numpy as np
import logging
from bids.grabbids import BIDSLayout
import pandas
import re
import os


class MP2RAGE(object):

    """ This object can calculate a Unified T1-weighted image and a
    quantitative T1 map, based on the magnitude and phase-information of the two
    volumes of a MP2RAGE-sequence (Marques et al., 2010).

    It can also further correct this map for B1 inhomogenieties using a
    B1 map (Marques et al., 2014).

    Args:
        MPRAGE_tr (float): MP2RAGE TR in seconds
        invtimesAB (list of floats): Inversion times in seconds
        flipangleABdegree (list of floats): Flip angle of the two readouts in degrees
        nZslices (list of integers): Slices Per Slab * [PartialFourierInSlice-0.5  0.5]
        FLASH_tr (float): TR of the GRE readout
        sequence (string): Kind of sequence (default is 'normal')
        inversion_efficiency: inversion efficiency of the MP2RAGE PULSE (Default is 0.96, 
                              as measured on a Siemens system).
        B0 (float): Field strength in Tesla
        inv1_combined (filename or Nifti1Image, optional): Magnitude and phase image corresponding to
                                                           first inversion pulse. Should always consist
                                                           of two volumes.
        inv2_combined (filename or Nifti1Image, optional): Magnitude and phase image corresponding to
                                                           second inversion pulse. Should always consist
                                                           of two volumes.
        inv1 (filename or Nifti1Image, optional): Magnitude image of first inversion pulse.
                                                  Should always consist of one volume.
        inv1ph (filename or Nifti1Image, optional): Phase image of first inversion pulse.
                                                    Should always consist of one volume.
        inv2 (filename or Nifti1Image, optional): Magnitude image of second inversion pulse.
                                                  Should always consist of one volume.
        inv2ph (filename or Nifti1Image, optional): Phase image of second inversion pulse.
                                                    Should always consist of one volume.

    Attributes:
        t1 (Nifti1Image): Quantitative T1 map
        t1_uni (Nifti1Image): Bias-field corrected T1-weighted map

        t1_masked (Nifti1Image): Quantitative T1 map, masked 
        t1w_uni_masked (Nifti1Image): Bias-field corrected T1-weighted map, masked
    """

    def __init__(self, 
                 MPRAGE_tr=None,
                 invtimesAB=None,
                 flipangleABdegree=None,
                 nZslices=None,
                 FLASH_tr=None,
                 sequence='normal',
                 inversion_efficiency=0.96,
                 B0=7,
                 inv1_combined=None, 
                 inv2_combined=None, 
                 inv1=None, 
                 inv1ph=None, 
                 inv2=None, 
                 inv2ph=None): 



        if inv1_combined is not None:
            inv1_combined = image.load_img(inv1_combined, dtype=np.double)

            if inv1_combined.shape[3] != 2:
                raise Exception('inv1_combined should contain two volumes')

            if (inv1 is not None) or (inv1ph is not None):
                raise Exception('*Either* give inv1_combined *or* inv1 and inv1_ph.')

            self.inv1 = image.index_img(inv1_combined, 0)
            self.inv1ph = image.index_img(inv1_combined, 1)

        if inv2_combined is not None:
            inv2_combined = image.load_img(inv2_combined, dtype=np.double)

            if inv2_combined.shape[3] != 2:
                raise Exception('inv2_combined should contain two volumes')

            if (inv1 is not None) or (inv1ph is not None):
                raise Exception('*Either* give inv2_combined *or* inv2 and inv2_ph.')

            self.inv2 = image.index_img(inv2_combined, 0)
            self.inv2ph = image.index_img(inv2_combined, 1)

        if inv1 is not None:
            self.inv1 = image.load_img(inv1, dtype=np.double)

        if inv2 is not None:
            self.inv2 = image.load_img(inv2, dtype=np.double)

        if inv1ph is not None:
            self.inv1ph = image.load_img(inv1ph, dtype=np.double)

        if inv2ph is not None:
            self.inv2ph = image.load_img(inv2ph, dtype=np.double)


        # Normalize phases between 0 and 2 pi
        self.inv1ph = image.math_img('((x - np.max(x))/ - np.ptp(x)) * 2 * np.pi', x=self.inv1ph)
        self.inv2ph = image.math_img('((x - np.max(x))/ - np.ptp(x)) * 2 * np.pi', x=self.inv2ph)

        # Set parameters
        self.MPRAGE_tr = MPRAGE_tr
        self.invtimesAB = invtimesAB
        self.flipangleABdegree = flipangleABdegree
        self.nZslices = nZslices
        self.FLASH_tr = FLASH_tr
        self.sequence = sequence
        self.inversion_efficiency = inversion_efficiency
        self.B0 = B0
        
        # set up t1
        self._t1 = None

        # Preset masked versions
        self._t1w_uni = None
        self._mask = None
        self._inv1_masked = None
        self._inv2_masked = None
        self._t1_masked = None
        self._t1w_uni_masked = None


    @property
    def t1w_uni(self):
        if self._t1w_uni is None:
            self.fit_t1w_uni()

        return self._t1w_uni

    @property
    def t1(self):
        if self._t1 is None:
            self.fit_t1()

        return self._t1
    
    def fit_t1w_uni(self):
        compINV1 = self.inv1.get_data() * np.exp(self.inv1ph.get_data() * 1j)
        compINV2 = self.inv2.get_data() * np.exp(self.inv2ph.get_data() * 1j)

        # Scale to 4095
        self._t1w_uni = (np.real(compINV1*compINV2/(compINV1**2 + compINV2**2)))*4095+2048

        # Clip anything outside of range
        self._t1w_uni = np.clip(self._t1w_uni, 0, 4095)

        # Convert to nifti-image
        self._t1w_uni = nb.Nifti1Image(self._t1w_uni, self.inv1.affine)

        return self._t1w_uni

    def fit_t1(self):
        if (self.MPRAGE_tr is None) or (self.invtimesAB is None) or (self.flipangleABdegree is None) \
                or (self.nZslices is None) or (self.FLASH_tr is None):
            raise Exception("All sequence parameters (MPRAGE_tr, invtimesAB, flipangleABdegree, nZslices,' \
                            ' and FLASH_TR) have to be provided for T1 fitting")
        
        Intensity, T1Vector, _ = MP2RAGE_lookuptable(self.MPRAGE_tr, self.invtimesAB, self.flipangleABdegree, 
                                                     self.nZslices, self.FLASH_tr, self.sequence, 2,
                                                     self.inversion_efficiency, self.B0)
        
        T1Vector = np.append(T1Vector, T1Vector[-1] + (T1Vector[-1]-T1Vector[-2]))    
        Intensity = np.append(Intensity, -0.5)
        
        
        T1Vector = T1Vector[np.argsort(Intensity)]
        Intensity = np.sort(Intensity)
        
        self._t1 = np.interp(-0.5 + self.t1w_uni.get_data()/4096, Intensity, T1Vector)
        self._t1[np.isnan(self._t1)] = 0
        
        # Convert to milliseconds
        self._t1 *= 1000
        
        # Make image
        self._t1 = nb.Nifti1Image(self._t1, self.t1w_uni.affine)
        
        return self._t1



    def fit_mask(self, modality='INV2', smooth_fwhm=2.5, threshold=None, **kwargs):
        """Fit a mask based on one of the MP2RAGE images (usually INV2).

        This function creates a mask of the brain and skull, so that parts of the image
        that have insufficient signal for proper T1-fitting can be ignored.
        By default, it uses a slightly smoothed version of the INV2-image (to increase
        SNR), and the "Nichols"-method, as implemented in the ``nilearn``-package,
        to remove low-signal areas. The "Nichols"-method looks for the lowest 
        density in the intensity histogram and places a threshold cut there.

        You can also give an arbitrary 'threshold'-parameter to threshold the image
        at a specific value.

        The resulting mask is returned and stored in the ``mask``-attribute of
        the MP2RAGEFitter-object. 

        Args:
            modality (str): Modality to  use for masking operation (defaults to INV2) 
            smooth (float): The size of the smoothing kernel to apply in mm (defaults 
                            to 2.5 mm)
            threshold (float): If not None, the image is thresholded at this (arbitary)
                               number.
            **kwargs: These arguments are forwarded to nilearn's ``compute_epi_mask``

        Returns:
            The computed mask

        """

        im = getattr(self, modality.lower())

        if threshold is None:
            smooth_im = image.smooth_img(im, smooth_fwhm)
            self._mask = masking.compute_epi_mask(smooth_im, **kwargs)
        else:
            self._mask = image.math_img('im > %s' % threshold, im=im)

        return self.mask

    @property
    def mask(self):
        if self._mask is None:
            logging.warning('Mask is not computed yet. Computing the mask now with' \
                            'default settings using nilearn\'s compute_epi_mask)' \
                            'For more control, use the ``fit_mask``-function.')
            self.fit_mask()

        return self._mask


    @property
    def t1_masked(self):
        return image.math_img('t1 * mask', t1=self.t1, mask=self.mask)

    @property
    def t1w_uni_masked(self):
        return image.math_img('t1w_uni * mask', t1w_uni=self.t1w_uni, mask=self.mask)

    @property
    def inv1_masked(self):
        return image.math_img('inv1 * mask', inv1=self.inv1, mask=self.mask)

    @property
    def inv2_masked(self):
        return image.math_img('inv2 * mask', inv2=self.inv2, mask=self.mask)


    @classmethod
    def from_bids(cls, source_dir, subject, **kwargs):
        """ Creates a MP2RAGE-object from a properly organized BIDS-folder.

        The folder should be organized as follows:

        sub-01/anat/:
         * sub-01_inv-1_part-mag_MP2RAGE.nii
         * sub-01_inv-1_part-phase_MP2RAGE.nii
         * sub-01_inv-2_part-mag_MP2RAGE.nii
         * sub-01_inv-2_part-phase_MP2RAGE.nii
         * sub-01_inv-1_MP2RAGE.json
         * sub-01_inv-2_MP2RAGE.json

         The JSON-files should contain all the necessary MP2RAGE sequence parameters
         and should look something like this:

         sub-01/anat/sub-01_inv-1_MP2RAGE.json:
             {
                "InversionTime":0.8,
                "FlipAngle":5,
                "ReadoutRepetitionTime":0.0062,
                "InversionRepetitionTime":5.5,
                "NumberShots":159
             }

         sub-01/anat/sub-01_inv-2_MP2RAGE.json:
             {
                "InversionTime":2.7,
                "FlipAngle":7,
                "ReadoutRepetitionTime":0.0062,
                "InversionRepetitionTime":5.5,
                "NumberShots":159
             }

        A MP2RAGE-object can now be created from the BIDS folder as follows:

        Example:
            >>> import pymp2rage
            >>> mp2rage = pymp2rage.MP2RAGE.from_bids('/data/sourcedata/', '01')

        Args:
            source_dir (BIDS dir): directory containing all necessary files
            subject (str): subject identifier
            **kwargs: additional keywords that are forwarded to get-function of
            BIDSLayout. For example `ses` could be used to select specific session.
        """



        layout = BIDSLayout(source_dir)
        
        filenames = layout.get(subject=subject, return_type='file', type='MP2RAGE', extensions=['.nii', '.nii.gz'], **kwargs)
        
        part_regex = re.compile('part-(mag|phase)')
        inv_regex = re.compile('inv-([0-9]+)')
        
        parts = [part_regex.search(fn).group(1) if part_regex.search(fn) else None for fn in filenames]
        inversion_idx = [int(inv_regex.search(fn).group(1)) if inv_regex.search(fn) else None for fn in filenames]
        
        # Check whether we have everything
        df = pandas.DataFrame({'fn':filenames, 
                               'inv':inversion_idx,
                               'part':parts})
        
        tmp = df[np.in1d(df.inv, [1, 2]) & np.in1d(df.part, ['mag', 'phase'])]
        check = (len(tmp) == 4) & (tmp.groupby(['inv', 'part']).size() == 1).all()
        
        if not check:
            raise ValueError('Did not find exactly one Magnitude and phase image for two' \
                             'inversions. Only found: %s' % tmp.fn.tolist())
        
        
        df = df.set_index(['inv', 'part'])
        
        inv1 = df.loc[1, 'mag'].fn
        inv1ph = df.loc[1, 'phase'].fn
        inv2 = df.loc[2, 'mag'].fn
        inv2ph = df.loc[2, 'phase'].fn
        
        meta_inv1 = layout.get_metadata(inv1)
        meta_inv2 = layout.get_metadata(inv2)
        
        for key in ['InversionRepetitionTime', 'NumberShots', 'PartialFourier']:
            if key in meta_inv1:
                if meta_inv1[key] != meta_inv2[key]:
                    raise ValueError('%s of INV1 and INV2 are different!' % key)        
        
        MPRAGE_tr = meta_inv1['InversionRepetitionTime']    
        invtimesAB = [meta_inv1['InversionTime'], meta_inv2['InversionTime']]    
        flipangleABdegree = [meta_inv1['FlipAngle'], meta_inv2['FlipAngle']]
        
        if 'PartialFourier' in meta_inv1.keys():
            nZslices = meta_inv1['NumberShots'] * np.array([meta_inv1['PartialFourier'] -.5, 0.5])    
        else: 
            nZslices = meta_inv1['NumberShots']
            
        FLASH_tr = [meta_inv1['ReadoutRepetitionTime'], meta_inv2['ReadoutRepetitionTime']]
        
        B0 = meta_inv1.pop('FieldStrength', 7)
        
        return cls(MPRAGE_tr,
                   invtimesAB,
                   flipangleABdegree,
                   nZslices,
                   FLASH_tr,
                   inv1=inv1,
                   inv1ph=inv1ph,
                   inv2=inv2,
                   inv2ph=inv2ph)


    def write_files(self, path=None, prefix=None, compress=True, masked=False):
        """ Write bias-field corrected T1-weighted image and T1 map to disk 
        as Nifti-files.

        If no filename or directory are given, the filename of INV1 is used
        as a template.

        The resulting files have the following names:
         * <path>/<prefix>_T1.nii[.gz]
         * <path>/<prefix>_T1w.nii[.gz]
         * [<path>/<prefix>_T1_masked.nii[.gz]]
         * [<path>/<prefix>_T1w_masked.nii[.gz]]
        
        Args:
            path (str, Optional): Directory where files should be placed
            prefix (str, Optional): Prefix of final filename (<path>/


        Example:
            >>> import pymp2rage
            >>> mp2rage = pymp2rage.MP2RAGE.from_bids('/data/sourcedata', '01')
            >>> mp2rage.write_files() # This write sub-01_T1w.nii.gz and 
                                      # sub-01_T1map.nii.gz to 
                                      # /data/sourcedata/sub-01/anat

        """

        if path is None:
            path = os.path.dirname(self.inv1.get_filename())
        

        if prefix is None:
            prefix = os.path.split(self.inv1.get_filename())[-1]

            INV_reg = re.compile('_?(INV)-?(1|2)', re.IGNORECASE)
            part_reg = re.compile('_?(part)-?(mag|phase)', re.IGNORECASE)
            MP2RAGE_reg = re.compile('_MP2RAGE', re.IGNORECASE)

            for reg in [INV_reg, part_reg, MP2RAGE_reg]:
                prefix = reg.sub('', prefix)

            prefix = os.path.splitext(prefix)[0]

        ext = '.nii.gz' if compress else '.nii'

        t1_filename = os.path.join(path, prefix+'_T1map'+ext)
        print("Writing T1 map to %s" % t1_filename)
        self.t1.to_filename(t1_filename)

        t1w_uni_filename = os.path.join(path, prefix+'_T1w'+ext)
        print("Writing bias-field corrected T1-weighted image to %s" % t1w_uni_filename)
        self.t1w_uni.to_filename(t1w_uni_filename)

        if masked:
            t1_masked_filename = os.path.join(path, prefix+'_T1map_masked'+ext)
            print("Writing masked T1 map to %s" % t1_masked_filename)
            self.t1_masked.to_filename(t1_masked_filename)

            t1w_uni_masked_filename = os.path.join(path, prefix+'_T1w_masked'+ext)
            print("Writing masked bias-field corrected T1-weighted image to %s" % t1w_uni_masked_filename)
            self.t1w_uni_masked.to_filename(t1w_uni_masked_filename)


    def plot_MP2RAGEproperties(self):
        
        """ This function replicates the plot_MP2RAGEproperties-function
        of the Matlab script by José Marques.
        
        It shows what effect different B1 differences as compared to intended
        flip angle has on the resulting contrast between gray matter (GM),
        white matter (WM), and cerebrospinal fluid (CSF).
        
        
        see:
        https://github.com/JosePMarques/MP2RAGE-related-scripts/blob/master/func/plotMP2RAGEproperties.m"""
        
        
        Signalres = lambda x1, x2: x1*x2/(x2**2+x1**2)
        noiseres = lambda x1, x2: ((x2**2-x1**2)**2 / (x2**2 + x1**2)**3 )**(0.5)

        Contrast = []

        if self.B0 == 3:
            T1WM=0.85
            T1GM=1.35
            T1CSF=2.8
            B1range=np.arange(0.8, 1.21, 0.1)
        else:
            T1WM=1.1
            T1GM=1.85
            T1CSF=3.9
            B1range=np.arange(0.6, 1.41, 0.2)
            
        lines = []

        for B1 in B1range:
            
            effective_flipangle = B1 * np.array(self.flipangleABdegree)
            MP2RAGEamp, T1vector, IntensityBeforeComb = MP2RAGE_lookuptable(self.MPRAGE_tr, 
                                                                            self.invtimesAB, effective_flipangle, 
                                                                            self.nZslices, self.FLASH_tr, 
                                                                            self.sequence, nimages=2,
                                                                            inversion_efficiency=self.inversion_efficiency, 
                                                                            B0=self.B0, all_data=1)
            

            lines.append(plt.plot(MP2RAGEamp, T1vector, color=np.array([0.5]*3)*B1, label='B1 = %.2f' % B1))
            posWM= np.argmin(np.abs(T1WM - T1vector))
            posGM= np.argmin(np.abs(T1GM - T1vector))
            posCSF = np.argmin(np.abs(T1CSF- T1vector))

            Signal= Signalres(IntensityBeforeComb[[posWM,posGM,posCSF],0], IntensityBeforeComb[[posWM,posGM,posCSF],1])
            noise = noiseres(IntensityBeforeComb[[posWM,posGM,posCSF],0],IntensityBeforeComb[[posWM,posGM,posCSF],1])


            Contrast.append(1000 * np.sum((Signal[1:]-Signal[:-1])/np.sqrt(noise[1:]**2+noise[:-1]**2))/np.sqrt(self.MPRAGE_tr))
            
            
        plt.axhline(T1CSF, color='red')
        plt.axhline(T1GM, color='green')
        plt.axhline(T1WM, color='blue')
        
        plt.text(0.35,T1WM,'White Matter')
        plt.text(0.35,T1GM,'Grey Matter')
        
        plt.text(-0.3,(T1CSF+T1GM)/2, 'Contrast over B1 range', va='center')
        plt.text(0,(T1CSF+T1GM)/2,'\n'.join(['%.2f' % c for c in Contrast]), va='center')
        
        plt.legend(loc='upper right')
        
        
        return Contrast


def MPRAGEfunc_varyingTR(MPRAGE_tr, inversiontimes, nZslices, 
                          FLASH_tr, flipangle, sequence, T1s, 
                          nimages=2,
                          B0=7, M0=1, inversionefficiency=0.96):

    if sequence == 'normal':
        normalsequence = True
        waterexcitation = False
    else:
        normalsequence = False
        waterexcitation = True

    nZslices = np.atleast_1d(nZslices)
    inversiontimes = np.atleast_1d(inversiontimes)
    FLASH_tr = np.atleast_1d(FLASH_tr)
    flipangle = np.atleast_1d(flipangle)

    FatWaterCSppm=3.3 # ppm
    gamma=42.576 #MHz/T
    pulseSpace=1/2/(FatWaterCSppm*B0*gamma) #

    fliprad = flipangle/180*np.pi

    if len(fliprad) != nimages:
        fliprad = np.repeat(fliprad, nimages)

    if len(FLASH_tr) != nimages:
        FLASH_tr = np.repeat(FLASH_tr, nimages)        

    if len(nZslices) == 2:
        nZ_bef=nZslices[0]
        nZ_aft=nZslices[1]
        nZslices=sum(nZslices);

    elif len(nZslices)==1:
        nZ_bef=nZslices / 2
        nZ_aft=nZslices / 2

    if normalsequence:
        E_1 = np.exp(-FLASH_tr / T1s)
        TA = nZslices * FLASH_tr
        TA_bef = nZ_bef * FLASH_tr
        TA_aft = nZ_aft * FLASH_tr

        TD = np.zeros(nimages+1)
        E_TD = np.zeros(nimages+1)

        TD[0] = inversiontimes[0]-TA_bef[0]
        E_TD[0] = np.exp(-TD[0] / T1s)

        TD[nimages] =MPRAGE_tr - inversiontimes[nimages-1] - TA_aft[-1]
        E_TD[nimages] = np.exp(-TD[nimages] / T1s)


        if nimages > 1:
            TD[1:nimages] = inversiontimes[1:] - inversiontimes[:-1] - (TA_aft[:-1] + TA_bef[1:])
            E_TD[1:nimages] = np.exp(-TD[1:nimages] / T1s)

        cosalfaE1 = np.cos(fliprad) * E_1    
        oneminusE1 = 1 - E_1
        sinalfa = np.sin(fliprad)

    MZsteadystate = 1. / (1 + inversionefficiency * (np.prod(cosalfaE1))**(nZslices) * np.prod(E_TD))

    MZsteadystatenumerator = M0 * (1 - E_TD[0])


    for i in np.arange(nimages):
        MZsteadystatenumerator = MZsteadystatenumerator*cosalfaE1[i]**nZslices + M0 * (1-E_1[i]) * (1-(cosalfaE1[i])**nZslices) / (1-cosalfaE1[i])        
        MZsteadystatenumerator = MZsteadystatenumerator*E_TD[i+1]+M0*(1-E_TD[i+1])

    MZsteadystate = MZsteadystate * MZsteadystatenumerator


    signal = np.zeros(nimages)

    m = 0
    temp = (-inversionefficiency*MZsteadystate*E_TD[m] + M0 * (1-E_TD[m])) * (cosalfaE1[m])**(nZ_bef) + \
           M0 * (1 - E_1[m]) * (1 - (cosalfaE1[m])**(nZ_bef)) \
           / (1-(cosalfaE1[m]))

    signal[0] = sinalfa[m] * temp


    for m in range(1, nimages):
        temp = temp * (cosalfaE1[m-1])**(nZ_aft) + \
               M0 * (1 - E_1[m-1]) * (1 - (cosalfaE1[m-1])**(nZ_aft)) \
              / (1-(cosalfaE1[m-1]))

        temp = (temp * E_TD[m] + M0 * (1 - E_TD[m])) * (cosalfaE1[m])**(nZ_bef) + \
               M0 * (1-E_1[m]) * (1 - (cosalfaE1[m])**(nZ_bef)) \
               / (1 - (cosalfaE1[m]))

        signal[m] = sinalfa[m]*temp

    return signal        

def MP2RAGE_lookuptable(MPRAGE_tr, invtimesAB, flipangleABdegree, nZslices, FLASH_tr, 
                     sequence, nimages=2, B0=7, M0=1, inversion_efficiency=0.96, all_data=0):
# first extra parameter is the inversion efficiency
# second extra parameter is the alldata
#   if ==1 all data is shown
#   if ==0 only the monotonic part is shown



    invtimesa, invtimesb = invtimesAB
    B1vector = 1

    flipanglea, flipangleb = flipangleABdegree

    T1vector = np.arange(0.05, 4.05, 0.05)

    FLASH_tr = np.atleast_1d(FLASH_tr)

    if len(FLASH_tr) == 1:
        FLASH_tr = np.repeat(FLASH_tr, nimages)


    nZslices = np.atleast_1d(nZslices)

    if len(nZslices)==2:        
        nZ_bef, nZ_aft = nZslices
        nZslices2 = np.sum(nZslices)

    elif len(nZslices) == 1:
        nZ_bef = nZ_aft = nZslices / 2
        nZslices2 = nZslices

    Signal = np.zeros((len(T1vector), 2))

    for j, T1 in enumerate(T1vector):
        if ((np.diff(invtimesAB) >= nZ_bef * FLASH_tr[1] + nZ_aft*FLASH_tr[0]) and \
           (invtimesa >= nZ_bef*FLASH_tr[0]) and \
           (invtimesb <= (MPRAGE_tr-nZ_aft*FLASH_tr[1]))):
            Signal[j, :] = MPRAGEfunc_varyingTR(MPRAGE_tr, invtimesAB, nZslices2, FLASH_tr, [flipanglea, flipangleb], sequence, T1, nimages, B0, M0, inversion_efficiency)


        else:
            Signal[j,:] = 0


    Intensity = np.squeeze(np.real(Signal[..., 0] * np.conj(Signal[..., 1])) / (np.abs(Signal[... ,0])**2 + np.abs(Signal[...,1])**2))

    if all_data == 0:
        minindex = np.argmax(Intensity)
        maxindex = np.argmin(Intensity)
        Intensity = Intensity[minindex:maxindex+1]
        T1vector = T1vector[minindex:maxindex+1]
        IntensityBeforeComb = Signal[minindex:maxindex+1]
    else:
        IntensityBeforeComb = Signal
    return Intensity, T1vector, IntensityBeforeComb
