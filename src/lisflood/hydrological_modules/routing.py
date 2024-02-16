"""

Copyright 2019 European Union

Licensed under the EUPL, Version 1.2 or as soon they will be approved by the European Commission  subsequent versions of the EUPL (the "Licence");

You may not use this work except in compliance with the Licence.
You may obtain a copy of the Licence at:

https://joinup.ec.europa.eu/sites/default/files/inline-files/EUPL%20v1_2%20EN(1).txt

Unless required by applicable law or agreed to in writing, software distributed under the Licence is distributed on an "AS IS" basis,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the Licence for the specific language governing permissions and limitations under the Licence.

"""
from __future__ import print_function, absolute_import

from pcraster import lddmask, accuflux, boolean, downstream, pit, path, lddrepair, ifthenelse, cover, nominal, uniqueid, \
    catchment, upstream, pcr2numpy

import numpy as np

from .lakes import lakes
from .reservoir import reservoir
from .polder import polder
from .inflow import inflow
from .transmission import transmission
from .kinematic_wave_parallel import kinematicWave, kwpt

from ..global_modules.settings import LisSettings, MaskInfo
from ..global_modules.add1 import loadmap, loadmap_base, compressArray, decompress
from . import HydroModule


class routing(HydroModule):

    """
    # ************************************************************
    # ***** ROUTING      *****************************************
    # ************************************************************
    """
    input_files_keys = {'all': ['beta', 'ChanLength', 'Ldd', 'Channels', 'ChanGrad', 'ChanGradMin',
                                'CalChanMan', 'ChanMan', 'ChanBottomWidth', 'ChanDepthThreshold',
                                'ChanSdXdY', 'TotalCrossSectionAreaInitValue', 'PrevDischarge'],
                        'SplitRouting': ['CrossSection2AreaInitValue', 'PrevSideflowInitValue', 'CalChanMan2'],
                        'dynamicWave': ['ChannelsDynamic'],
                        'MCTRouting': ['ChannelsMCT','PrevQMCTinInitValue','PrevQMCToutInitValue','PrevCmMCTInitValue',
                                       'PrevDmMCTInitValue']}
    module_name = 'Routing'

    def __init__(self, routing_variable):
        self.var = routing_variable

        self.lakes_module = lakes(self.var)
        self.reservoir_module = reservoir(self.var)
        self.polder_module = polder(self.var)
        self.inflow_module = inflow(self.var)
        self.transmission_module = transmission(self.var)

# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
    def initial(self):
        """ initial part of the routing module
        """
        maskinfo = MaskInfo.instance()
        self.var.avgdis = maskinfo.in_zero()
        #cmcheck -> this is a fixed exponent 3/5 from Manning's equation! No need to read it from a map.
        self.var.Beta = loadmap('beta')
        self.var.InvBeta = 1 / self.var.Beta
        # Inverse of beta for kinematic wave
        self.var.ChanLength = loadmap('ChanLength').astype(float)
        self.var.InvChanLength = 1 / self.var.ChanLength
        # Inverse of channel length [1/m]

        self.var.NoRoutSteps = int(np.maximum(1, round(self.var.DtSec / self.var.DtSecChannel,0)))
        # Number of sub-steps based on value of DtSecChannel,
        # or 1 if DtSec is smaller than DtSecChannel
        settings = LisSettings.instance()
        option = settings.options
        if option['InitLisflood']:
            self.var.NoRoutSteps = 1
            # InitLisflood is used!
            # so channel routing step is the same as the general time step
        self.var.DtRouting = self.var.DtSec / self.var.NoRoutSteps
        # Corresponding sub-timestep (seconds)
        self.var.InvDtRouting = 1 / self.var.DtRouting
        self.var.InvNoRoutSteps = 1 / float(self.var.NoRoutSteps)
        # inverse for faster calculation inside the dynamic section

        # ************************************************************
        # ***** DRAINAGE NETWORK GEOMETRY - LDD  *********************
        # ************************************************************

        self.var.Ldd = lddmask(loadmap('Ldd', pcr=True, lddflag=True), self.var.MaskMap)    #pcr
        # Cut ldd to size of MaskMap (NEW, 29/9/2004)
        # Prevents 'unsound' ldd if MaskMap covers sub-area of ldd

        self.var.UpArea = accuflux(self.var.Ldd, self.var.PixelAreaPcr)     #pcr
        # Upstream contributing area for each pixel
        # Note that you might expext that values of UpArea would be identical to
        # those of variable CatchArea (see below) at the outflow points.
        # This is NOT actually the case, because outflow points are shifted 1
        # cell in upstream direction in the calculation of CatchArea!
        self.var.InvUpArea = 1 / self.var.UpArea    #pcr
        # Count (inverse of) upstream area for each pixel
        # Needed if we want to calculate average values of variables
        # upstream of gauge locations
        # Calculate inverse, so we can multiply in dynamic (faster than divide)

        self.var.IsChannelPcr = boolean(loadmap('Channels', pcr=True))  #pcr
        self.var.IsChannel = np.bool8(compressArray(self.var.IsChannelPcr))     #bool
        # Identify channel pixels
        self.var.IsStructureChan = np.bool8(maskinfo.in_zero())        #bool
        # Initialise map that identifies special inflow/outflow structures (reservoirs, lakes) within the
        # channel routing. Set to (dummy) value of zero modified in reservoir and lake
        # routines (if those are used)

        self.var.IsChannelKinematic = self.var.IsChannel.copy()     #bool
        # Identify kinematic wave channel pixels
        # (identical to IsChannel, unless dynamic/MCT wave is used, see below)
        self.var.IsStructureKinematic = np.bool8(maskinfo.in_zero())        #bool
        # Initialise map that identifies special inflow/outflow structures (reservoirs, lakes) within the
        # kinematic wave channel routing. Set to (dummy) value of zero modified in reservoir and lake
        # routines (if those are used)


        LddChan = lddmask(self.var.Ldd, self.var.IsChannelPcr)  #pcr
        LddChanNp=compressArray(LddChan)    #np
        # ldd for Channel network
        self.var.MaskMap = boolean(self.var.Ldd)    #pcr
        self.var.MaskMapNp=compressArray(self.var.MaskMap)  #np
        # Use boolean version of Ldd as calculation mask
        # (important for correct mass balance check any water generated outside of Ldd won't reach channel anyway)
        self.var.LddToChan = lddrepair(ifthenelse(self.var.IsChannelPcr, 5, self.var.Ldd)) #pcr
        self.var.LddToChanNp=compressArray(self.var.LddToChan)  #np
        # Routing of runoff (incl. groundwater)
        AtOutflow = boolean(pit(self.var.Ldd))  #pcr
        AtOutflowNp=compressArray(AtOutflow)    #np
        # find outlet points...

        if option['dynamicWave']:
            IsChannelDynamic = boolean(loadmap('ChannelsDynamic', pcr=True))
            # Identify channel pixels where dynamic wave is used
            self.var.IsChannelKinematic = (self.var.IsChannelPcr == 1) & (IsChannelDynamic == 0)
            # Identify (update) channel pixels where kinematic wave is used
            self.var.LddKinematic = lddmask(self.var.Ldd, self.var.IsChannelKinematic)
            # Ldd for kinematic wave: ends (pit) just before dynamic stretch

            # Following statements produce an ldd network that connects the pits in
            # LddKinematic to the nearest downstream dynamic wave pixel

            self.var.AtLastPoint = (downstream(self.var.Ldd, AtOutflow) == 1) & (AtOutflow != 1) & self.var.IsChannelPcr

            # NEW 23-6-2005
            # Dynamic wave routine gives no outflow out of pits, so we calculate this
            # one cell upstream (WvD)
            # (implies that most downstream cell is not taken into account in mass balance
            # calculations, even if dyn wave is not used)
            # Only include points that are on a channel (otherwise some small 'micro-catchments'
            # are included, for which the mass balance cannot be calculated
            # properly)

        elif option['MCTRouting']:
            #print('MCTRouting setting LDD')
            self.var.IsChannelMCTPcr = boolean(loadmap('ChannelsMCT', pcr=True))    #pcr
            self.var.IsChannelMCT = np.bool8(compressArray(self.var.IsChannelMCTPcr))   #bool
            # Identify channel pixels where Muskingum-Cunge-Todini is used
            self.var.mctmask = np.bool8(pcr2numpy(self.var.IsChannelMCTPcr,0))
            # mask with cells using MCT

            self.var.IsChannelKinematicPcr = (self.var.IsChannelPcr == 1) & (self.var.IsChannelMCTPcr == 0)  #pcr
            self.var.IsChannelKinematic = np.bool8(compressArray(self.var.IsChannelKinematicPcr))   #np
            # Identify channel pixels where Kinematic wave is used

            self.var.LddMCT = lddmask(self.var.Ldd, self.var.IsChannelMCTPcr)  #pcr
            self.var.LddMCTNp = compressArray(self.var.LddMCT)    #np
            # Ldd for MCT routing

            self.var.LddKinematic = lddmask(self.var.Ldd, self.var.IsChannelKinematicPcr)    #pcr
            # Ldd for kinematic routing

        else:
            self.var.LddKinematic = LddChan
            # No dynamic/MCT routing, so kinematic ldd equals channel ldd

        self.var.LddKinematicNp = compressArray(self.var.LddKinematic)    #np

        self.var.LddChan = LddChan  #pcr
        self.var.LddChanNp = compressArray(self.var.LddChan)    #np

        self.var.AtLastPoint = AtOutflow
        #AtOutflowNp=compressArray(AtOutflow)    #np
        self.var.AtLastPointC = np.bool8(compressArray(self.var.AtLastPoint))
        # assign unique identifier to each of the outlet points

        maskinfo = MaskInfo.instance()
        #lddC = compressArray(self.var.LddKinematic)     #np
        lddC = compressArray(LddChan)     #np
        inAr = decompress(np.arange(maskinfo.info.mapC[0], dtype="int32"))  #pcr
        inArNp=compressArray(inAr)  #np
        # giving a number to each non missing pixel as id

        #self.var.downstruct = (compressArray(downstream(self.var.LddKinematic, inAr))).astype("int32")  #np
        self.var.downstruct = (compressArray(downstream(LddChan, inAr))).astype("int32")  #np
        # each upstream pixel gets the id of the downstream pixel
        self.var.downstruct[lddC == 5] = maskinfo.info.mapC[0]  #np
        # all pits get a high number than any of the cells
        # upstream function in numpy

        OutflowPoints = nominal(uniqueid(self.var.AtLastPoint))     #pcr
        OutflowPointsNp = compressArray(OutflowPoints)      #np
        # and assign unique identifier to each of them
        # assigning id to the outflow points starting from 1
        self.var.Catchments = (compressArray(catchment(self.var.Ldd, OutflowPoints))).astype(np.int32)  #np
        # assign outlet id to all pixel in its catchment
        # define catchment for each outflow point
        CatchArea = np.bincount(self.var.Catchments, weights=self.var.PixelArea)[self.var.Catchments]   #np
        # Compute area of each catchment [m2]
        # Note: in earlier versions this was calculated using the "areaarea" function,
        # changed to "areatotal" in order to enable handling of grids with spatially
        # variable cell areas (e.g. lat/lon grids)
        self.var.InvCatchArea = 1 / CatchArea
        # inverse of catchment area [1/m2]

        # ************************************************************
        # ***** CHANNEL GEOMETRY  ************************************
        # ************************************************************

        self.var.ChanGrad = np.maximum(loadmap('ChanGrad'), loadmap('ChanGradMin'))
        # avoid calculation of Alpha using ChanGrad=0: this creates MV!

        # cmcheck
        if option['MCTRouting']:
            # set channel slope for MCT pixels to max 0.001
            # Check where IsChannelMCT is True and values in ChanGrad > 0.001
            MCT_slope_mask = np.logical_and(self.var.IsChannelMCT, self.var.ChanGrad > 0.001)
            # Update values in ChanGrad where the condition is met
            self.var.ChanGrad[MCT_slope_mask] = 0.001


        self.var.CalChanMan = loadmap('CalChanMan')
        self.var.ChanMan = self.var.CalChanMan * loadmap('ChanMan')
        # Manning's n is multiplied by ChanManCal
        # enables calibration for peak timing
        self.var.ChanBottomWidth = loadmap('ChanBottomWidth')
        ChanDepthThreshold = loadmap('ChanDepthThreshold')
        ChanSdXdY = loadmap('ChanSdXdY')
        self.var.ChanSdXdY = loadmap('ChanSdXdY')

        #######################cm
        # self.var.ChanSdXdY = self.var.ChanSdXdY * 0               # sezione rettangolare
        # self.var.ChanBottomWidth = self.var.ChanBottomWidth * 0   # sezione tringolare
        #######################cm

        self.var.ChanUpperWidth = self.var.ChanBottomWidth + 2 * ChanSdXdY * ChanDepthThreshold
        # Channel upper width [m]
        self.var.TotalCrossSectionAreaBankFull = 0.5 * \
            ChanDepthThreshold * (self.var.ChanUpperWidth + self.var.ChanBottomWidth)
        # Area (sq m) of bank full discharge cross section [m2]
        # (trapezoid area equation)

        #cmcheck -> half area is wrong Area at half bankfull is NOT half of the area at bankfull
        # ChanUpperWidthHalfBankFull = self.var.ChanBottomWidth + 2 * ChanSdXdY * 0.5 * ChanDepthThreshold
        # TotalCrossSectionAreaHalfBankFull = 0.5 * \
        #     0.5 * ChanDepthThreshold * (ChanUpperWidthHalfBankFull + self.var.ChanBottomWidth)
        TotalCrossSectionAreaHalfBankFull = 0.5 * self.var.TotalCrossSectionAreaBankFull
        # Cross-sectional area at half bankfull [m2]
        # This can be used to initialise channel flow (see below)

        TotalCrossSectionAreaInitValue = loadmap('TotalCrossSectionAreaInitValue')
        self.var.TotalCrossSectionArea = np.where(TotalCrossSectionAreaInitValue == -9999, TotalCrossSectionAreaHalfBankFull, TotalCrossSectionAreaInitValue)
        # Total cross-sectional area [m2]: if initial value in binding equals -9999 the value at half bankfull is used,
        # otherwise TotalCrossSectionAreaInitValue (typically end map from previous simulation)

        if option['SplitRouting']:
            # in_zero = maskinfo.in_zero()
            CrossSection2AreaInitValue = loadmap('CrossSection2AreaInitValue')
            self.var.CrossSection2Area = np.where(CrossSection2AreaInitValue == -9999, maskinfo.in_zero(), CrossSection2AreaInitValue)
            # cross-sectional area [m2] for 2nd line of routing (over bankfull only): if initial value in binding equals -9999 the value is set to 0
            # otherwise CrossSection2AreaInitValue (typically end map from previous simulation)

            PrevSideflowInitValue = loadmap('PrevSideflowInitValue')
            self.var.Sideflow1Chan = np.where(PrevSideflowInitValue == -9999, maskinfo.in_zero(), PrevSideflowInitValue)
            # sideflow from previous run for 1st line of routing: if initial value in binding equals -9999 the value is set to 0
            # otherwise PrevSideflowInitValue (typically end map from previous simulation)

        # ************************************************************
        # ***** CHANNEL ALPHA (KIN. WAVE)*****************************
        # ************************************************************
        # Following calculations are needed to calculate Alpha parameter in kinematic
        # wave. Alpha currently fixed at half of bankfull depth (this may change in
        # future versions!)
        # Manning's steady state flow equations
        # from Ven The Chow - Applied Hydrology - page 283
        # https: // wecivilengineers.files.wordpress.com / 2017 / 10 / applied - hydrology - ven - te - chow.pdf
        # A = Alpha * Q ** Beta
        # Q = (A/Alpha) ** (1/Beta) = (invAlpha * A)**invBeta
        ChanWaterDepthAlpha = np.where(self.var.IsChannel, 0.5 * ChanDepthThreshold, 0.0)
        # Reference water depth for calculation of Alpha: half of bankfull
        self.var.ChanWettedPerimeterAlpha = self.var.ChanBottomWidth + 2 * \
            np.sqrt(np.square(ChanWaterDepthAlpha) + np.square(ChanWaterDepthAlpha * ChanSdXdY))
        # Channel wetted perimeter half bankfull [m](Pythagoras)

        AlpTermChan = (self.var.ChanMan / (np.sqrt(self.var.ChanGrad))) ** self.var.Beta
        self.var.AlpPow = 2.0 / 3.0 * self.var.Beta # this is 2/5
        self.var.ChannelAlpha = (AlpTermChan * (self.var.ChanWettedPerimeterAlpha ** self.var.AlpPow)).astype(float)

        self.var.InvChannelAlpha = 1 / self.var.ChannelAlpha
        # ChannelAlpha for kinematic wave

        # ************************************************************
        # ***** CHANNEL INITIAL DISCHARGE ****************************
        # ************************************************************

        self.var.ChanM3 = self.var.TotalCrossSectionArea * self.var.ChanLength  #np
        # totalk river channel water volume [m3]
        self.var.ChanIniM3 = self.var.ChanM3.copy() #np
        self.var.ChanM3Kin = self.var.ChanIniM3.copy().astype(float)    #np
        # Initialise water volume in kinematic wave channels [m3]

        self.var.ChanQKin = np.where(self.var.ChannelAlpha > 0, (self.var.TotalCrossSectionArea / self.var.ChannelAlpha) ** self.var.InvBeta, 0).astype(float)
        # Initialise discharge at kinematic wave pixels (note that InvBeta is
        # simply 1/beta, computational efficiency!)

        self.var.CumQ = maskinfo.in_zero()
        # ininialise sum of discharge to calculate average

        # ************************************************************
        # ***** CHANNEL INITIAL DYNAMIC WAVE *************************
        # ************************************************************
        if option['dynamicWave']:
            pass
            # TODO !!!!!!!!!!!!!!!!!!!!

       #     lookchan = lookupstate(TabCrossSections, ChanCrossSections, ChanBottomLevel, self.var.ChanLength,
       #                            DynWaveConstantHeadBoundary + ChanBottomLevel)
       #     ChanIniM3 = ifthenelse(AtOutflow, lookchan, ChanIniM3)
            # Correct ChanIniM3 for constant head boundary in pit (only if
            # dynamic wave is used)
       #     ChanM3Dyn = ChanIniM3
            # Set volume of water in dynamic wave channel to initial value
            # (note that initial condition is expressed as a state in [m3] for the dynamic wave,
            # and as a rate [m3/s] for the kinematic wave (a bit confusing)

            # Estimate number of iterations needed in first time step (based on Courant criterium)
            # TO DO !!!!!!!!!!!!!!!!!!!!
        #    Potential = lookuppotential(
        #        TabCrossSections, ChanCrossSections, ChanBottomLevel, self.var.ChanLength, ChanM3Dyn)
            # Potential
        #    WaterLevelDyn = Potential - ChanBottomLevel
            # Water level [m above bottom level)
        #    WaveCelerityDyn = pcraster.sqrt(9.81 * WaterLevelDyn)
            # Dynamic wave celerity [m/s]
        #    CourantDynamic = self.var.DtSec * \
        #        (WaveCelerityDyn + 2) / self.var.ChanLength
            # Courant number for dynamic wave
            # We don't know the water velocity at this time so
            # we just guess it's 2 m/s (Odra tests show that flow velocity
            # is typically much lower than wave celerity, and 2 m/s is quite
            # high already so this gives a pretty conservative/safe estimate
            # for DynWaveIterations)
        #    DynWaveIterationsTemp = max(
        #        1, roundup(CourantDynamic / CourantDynamicCrit))
        #    DynWaveIterations = ordinal(mapmaximum(DynWaveIterationsTemp))
            # Number of sub-steps needed for required numerical
            # accuracy. Always greater than or equal to 1
            # (otherwise division by zero!)

            # TEST
            # If polder option is used, we need an estimate of the initial channel discharge, but we don't know this
            # for the dynamic wave pixels (since only initial state is known)! Try if this works (dyn wave flux based on zero inflow 1 iteration)
            # Note that resulting ChanQ is ONLY used in the polder routine!!!
            # Since we need instantaneous estimate at start of time step, a
            # ChanQM3Dyn is calculated for one single one-second time step!!!

        #    ChanQDyn = dynwaveflux(TabCrossSections,
        #                           ChanCrossSections,
        #                           LddDynamic,
        #                           ChanIniM3,
        #                           0.0,
        #                           ChanBottomLevel,
        #                           self.var.ChanMan,
        #                           self.var.ChanLength,
        #                           1,
        #                           1,
        #                           DynWaveBoundaryCondition)
            # Compute volume and discharge in channel after dynamic wave
            # ChanM3Dyn in [cu m]
            # ChanQDyn in [cu m / s]
        #    self.var.ChanQ = ifthenelse(
        #        IsChannelDynamic, ChanQDyn, self.var.ChanQKin)
            # Channel discharge: combine results of kinematic and dynamic wave
        else:

            # ***** NO DYNAMIC WAVE *************************
            # Dummy code if dynamic wave is not used, in which case ChanQ equals ChanQKin
            # (needed only for polder routine)

            PrevDischarge = loadmap('PrevDischarge')
            # outflow discharge at the end of previous step (instant)
            self.var.ChanQ = np.where(PrevDischarge == -9999, self.var.ChanQKin, PrevDischarge) #np
            # initialise channel discharge: cold start: equal to ChanQKin
            # [m3/s]
            # outflow at the end of previous time step (instant)


        # Initialising cumulative output variables
        # These are all needed to compute the cumulative mass balance error
        self.var.DischargeM3Out = maskinfo.in_zero()
        # cumulative discharge volume at outlet [m3]
        self.var.TotalQInM3 = maskinfo.in_zero()
        # cumulative inflow volume from inflow hydrographs [m3]
        self.var.sumDis = maskinfo.in_zero()
        self.var.sumIn = maskinfo.in_zero() #non so se sostituita da self.var.sumInWB
        self.var.sumInWB = maskinfo.in_zero()


    def initialSecond(self):
        """ initial part of the second channel routing module
        """
        settings = LisSettings.instance()
        option = settings.options
        binding = settings.binding

        self.var.ChannelAlpha2 = None  # default value, if split-routing is not active and water is routed only in the main channel
        # ************************************************************
        # ***** CHANNEL INITIAL SPLIT UP IN SECOND CHANNEL************
        # ************************************************************
        if option['SplitRouting']:

            ChanMan2 = (self.var.ChanMan / self.var.CalChanMan) * loadmap('CalChanMan2')
            AlpTermChan2 = (ChanMan2 / (np.sqrt(self.var.ChanGrad))) ** self.var.Beta
            self.var.ChannelAlpha2 = (AlpTermChan2 * (self.var.ChanWettedPerimeterAlpha ** self.var.AlpPow)).astype(float)
            #cmcheck -> using channel wetted perimeter of half bankfull ChanWettedPerimeterAlpha ?
            self.var.InvChannelAlpha2 = 1 / self.var.ChannelAlpha2
            # calculating second Alpha for second (virtual) channel

            if not(option['InitLisflood']):

                # use loadmap_base function as we don't want to cache avgdis it in the calibration
                self.var.QLimit = loadmap_base('AvgDis') * loadmap('QSplitMult')

                # Over bankful discharge starts at QLimit
                # lower discharge limit for second line of routing
                # set to mutiple of average discharge (map from prerun)
                # QSplitMult =2 is around 90 to 95% of Q

                self.var.M3Limit = self.var.ChannelAlpha * self.var.ChanLength * (self.var.QLimit ** self.var.Beta)
                # Water volume in bankful when over bankful discharge starts
                # Manning's equation

                # QLimit should NOT be dependent on the NoRoutSteps (number of routing steps)
                # self.var.QLimit = self.var.QLimit / self.var.NoRoutSteps #original

                ###############################################
                # CM mod
                # TEMPORARY WORKAROUND FOR EFAS XDOM!!!!!!!!!!
                # This must be removed
                # self.var.QLimit = self.var.QLimit / 24.0
                ###############################################

                self.var.Chan2M3Start = self.var.ChannelAlpha2 * self.var.ChanLength * (self.var.QLimit ** self.var.Beta)
                # virtual (note we use ChannelAlpha2 now) amount of water in the main channel at the activation of second line of routing 'floodplains' (=> start using increased Manning coeff)
                self.var.Chan2QStart = self.var.QLimit - compressArray(upstream(self.var.LddKinematic, decompress(self.var.QLimit)))
                # virtual outflow from main channel at the activation of second line of routing (=> start using increased Manning coeff)
                # because kinematic routing with a low amount of discharge leads to long travel time:
                # Starting Q for second line is set to a higher value

                self.var.Chan2M3Kin = self.var.CrossSection2Area * self.var.ChanLength + self.var.Chan2M3Start
                # Total (virtual) volume of water in river channel when second routing line is active (= using increased Manning coeff)
                self.var.ChanM3Kin = self.var.ChanM3 - self.var.Chan2M3Kin + self.var.Chan2M3Start
                # (Real) Volume of water in main channel when second line of routing is active (= using riverbed Manning coeff)
                
                self.var.ChanM3Kin = np.where((self.var.ChanM3Kin < 0.0) & (self.var.ChanM3Kin > -0.0000001),0.0,self.var.ChanM3Kin)
                # this line prevents the warm start from failing in case of small numerical imprecisions when writing and reading the maps

                self.var.Chan2QKin = (self.var.Chan2M3Kin * self.var.InvChanLength * self.var.InvChannelAlpha2) ** (self.var.InvBeta)
                # Total (virtual) outflow from river channel when second routing line is active (= using increased Manning coeff)
                self.var.ChanQKin = (self.var.ChanM3Kin * self.var.InvChanLength * self.var.InvChannelAlpha) ** (self.var.InvBeta)
                # (Real) outflow from main channel when second line of routing is active (= using riverbed Manning coeff)


        # Initialise parallel kinematic wave router: main channel-only routing if self.var.ChannelAlpha2 is None; else split-routing(main channel + floodplains)
        # Initialization includes LDD for kinematic routing
        maskinfo = MaskInfo.instance()
        self.river_router = kinematicWave(compressArray(self.var.LddKinematic), ~maskinfo.info.mask, self.var.ChannelAlpha,
                                           self.var.Beta, self.var.ChanLength, self.var.DtRouting,
                                           int(binding["numCPUs_parallelKinematicWave"]), alpha_floodplains=self.var.ChannelAlpha2)

        #   WATER BALANCE
        if option['InitLisflood'] and option['repMBTs']:
            # Calculate initial water storage in rivers (no lakes no reservoirs)
            self.var.StorageStepINIT= self.var.ChanM3Kin
            # Water volume in river channels
            self.var.DischargeM3StructuresIni = maskinfo.in_zero()
            if option['simulateReservoirs']:
               self.var.StorageStepINIT += self.var.ReservoirStorageIniM3
            if option['simulateLakes']:
               self.var.StorageStepINIT += self.var.LakeStorageIniM3
            self.var.StorageStepINIT = np.take(np.bincount(self.var.Catchments, weights=self.var.StorageStepINIT), self.var.Catchments)

        if not option['InitLisflood'] and option['repMBTs']:
           DisStructure = np.where(self.var.IsUpsOfStructureKinematicC, self.var.ChanQ * self.var.DtRouting, 0)
           # cmchek to fix IsUpsOfStructureKinematicC for MCT
           if not(option['SplitRouting']):
            self.var.StorageStepINIT = self.var.ChanM3Kin
            if option['simulateReservoirs']:
               self.var.StorageStepINIT += self.var.ReservoirStorageIniM3
               DisStructure = np.where(self.var.IsUpsOfStructureKinematicC, self.var.ChanQ * self.var.DtRouting, 0)
            if option['simulateLakes']:
               self.var.StorageStepINIT += self.var.LakeStorageIniM3
               DisStructure += np.where(compressArray(self.var.IsUpsOfStructureLake), 0.5 * self.var.ChanQ * self.var.DtRouting, 0)
            self.var.DischargeM3StructuresIni = np.take(np.bincount(self.var.Catchments, weights=DisStructure), self.var.Catchments)
           else:
            self.var.StorageStepINIT= self.var.ChanM3Kin+self.var.Chan2M3Kin-self.var.Chan2M3Start
            if option['simulateReservoirs']:
               self.var.StorageStepINIT += self.var.ReservoirStorageIniM3
            if option['simulateLakes']:
               self.var.StorageStepINIT += self.var.LakeStorageIniM3
            self.var.StorageStepINIT = np.take(np.bincount(self.var.Catchments, weights=self.var.StorageStepINIT), self.var.Catchments)

    def initialMCT(self):
        """ initial part of the Muskingum-Kunge-Todini routing module
        """
        settings = LisSettings.instance()
        option = settings.options
        binding = settings.binding

        self.var.ChannelAlpha2 = None  # default value, if split-routing is not active and water is routed only in the riverbed channel
        # ************************************************************
        # ***** INITIALISATION FOR MCT                    ************
        # ************************************************************
        if option['MCTRouting']:
            maskinfo = MaskInfo.instance()
            # initialisation for MCT routing
            PrevQMCTin = loadmap('PrevQMCTinInitValue')     # instant input discharge for MCT
            self.var.PrevQMCTin = np.where(PrevQMCTin == -9999, maskinfo.in_zero(), PrevQMCTin)  # np
            # MCT inflow (x) to MCT pixel at time t0

            PrevQMCTout = loadmap('PrevQMCToutInitValue')     # instant output discharge for MCT
            self.var.PrevQMCTout = np.where(PrevQMCTout == -9999, maskinfo.in_zero(), PrevQMCTout) #np
            # MCT outflow (x+dx) from MCT pixel at time t0

            PrevCmMCT = loadmap('PrevCmMCTInitValue')     # Cm for MCT
            self.var.PrevCm0 = np.where(PrevCmMCT == -9999, maskinfo.in_one(), PrevCmMCT) #np
            PrevDmMCT = loadmap('PrevDmMCTInitValue')     # Dm for MCT
            self.var.PrevDm0 = np.where(PrevDmMCT == -9999, maskinfo.in_zero(), PrevDmMCT) #np

            self.var.ChanQ = np.where(self.var.IsChannelKinematic, self.var.ChanQ, self.var.PrevQMCTout)
            #

            # Initialise mct wave router
            # self.mct_river_router = mctWave(compressArray(self.var.LddMCT), ~maskinfo.info.mask)

            self.mct_river_router = mctWave(self.get_mct_pix(compressArray(self.var.LddMCT)), self.var.mctmask)



# --------------------------------------------------------------------------
# --------------------------------------------------------------------------

    def dynamic(self, NoRoutingExecuted):
        """ dynamic part of the routing subtime module
        """
        settings = LisSettings.instance()
        option = settings.options
        binding = settings.binding

        if not(option['InitLisflood']):    # only with no InitLisflood
            self.lakes_module.dynamic_inloop(NoRoutingExecuted)
            self.reservoir_module.dynamic_inloop(NoRoutingExecuted)
            self.polder_module.dynamic_inloop()

        # End only with no Lisflood (no reservoirs, lakes and polder with
        # initLisflood)

        self.inflow_module.dynamic_inloop(NoRoutingExecuted)
        self.transmission_module.dynamic_inloop()

        # ************************************************************
        # ***** CHANNEL FLOW ROUTING: KINEMATIC WAVE  ****************
        # ************************************************************

        if not(option['dynamicWave']):

            # ************************************************************
            # ***** SIDEFLOW
            # ************************************************************

            SideflowChanM3 = self.var.ToChanM3RunoffDt.copy()

            if option['openwaterevapo']:
                SideflowChanM3 -= self.var.EvaAddM3Dt
            if option['wateruse']:
                self.var.WUseAddM3Dt = (self.var.withdrawal_CH_actual_M3_routStep - self.var.returnflow_GwAbs2Channel_M3_routStep)  
                SideflowChanM3 -= self.var.WUseAddM3Dt 
            if option['inflow']:
                SideflowChanM3 += self.var.QInDt
            if option['TransLoss']:
                SideflowChanM3 -= self.var.TransLossM3Dt
            if not(option['InitLisflood']):    # only with no InitLisflood
                if option['simulateLakes']:
                    SideflowChanM3 += self.var.QLakeOutM3Dt                  
                if option['simulateReservoirs']:
                    SideflowChanM3 += self.var.QResOutM3Dt                   
                if option['simulatePolders']:
                    SideflowChanM3 -= self.var.ChannelToPolderM3Dt
                    
                
                    
            ### check mass balance within routing ###
            if option['repMBTs']:  
             if (NoRoutingExecuted<1):
                 self.var.AddedTRUN = np.take(np.bincount(self.var.Catchments, weights=self.var.ToChanM3RunoffDt.copy()),self.var.Catchments)
                 if option['inflow']:
                     self.var.AddedTRUN += np.take(np.bincount(self.var.Catchments, weights=self.var.QInDt),self.var.Catchments)
                 if option['openwaterevapo']:
                     self.var.AddedTRUN -= np.take(np.bincount(self.var.Catchments, weights=self.var.EvaAddM3Dt.copy()),self.var.Catchments)
                 if option['wateruse']:
                     self.var.AddedTRUN -= np.take(np.bincount(self.var.Catchments, weights=self.var.WUseAddM3Dt.copy()),self.var.Catchments)
             else:
                 self.var.AddedTRUN += np.take(np.bincount(self.var.Catchments, weights=self.var.ToChanM3RunoffDt.copy()),self.var.Catchments)
                 if option['inflow']:
                     self.var.AddedTRUN += np.take(np.bincount(self.var.Catchments, weights=self.var.QInDt),self.var.Catchments) 
                 if option['openwaterevapo']:
                     self.var.AddedTRUN -= np.take(np.bincount(self.var.Catchments, weights=self.var.EvaAddM3Dt.copy()),self.var.Catchments)
                 if option['wateruse']:
                     self.var.AddedTRUN -= np.take(np.bincount(self.var.Catchments, weights=self.var.WUseAddM3Dt.copy()),self.var.Catchments)      
                                     
            # Runoff (surface runoff + flow out of Upper and Lower Zone), outflow from
            # reservoirs and lakes and inflow from external hydrographs are added to the channel
            # system (here in [cu m])
            #
            # NOTE: polders currently don't work with kinematic wave, but nevertheless
            # ChannelToPolderM3 is already included in sideflow term (so it's there in case
            # the polder routine is ever modified to make it work with kin. wave)
            # Because of wateruse Sideflow might get even smaller than 0,
            # instead of inflow its outflow
           
            SideflowChan = np.where(self.var.IsChannelKinematic, SideflowChanM3 * self.var.InvChanLength * self.var.InvDtRouting,0)
            # Sideflow expressed in [cu m /s / m channel length]

            # Calc sideflow for MCT cells
            if option['MCTRouting']:
                SideflowChanMCT = np.where(self.var.IsChannelMCT, SideflowChanM3 * self.var.InvDtRouting,0)     #Ql
            else:
                SideflowChanMCT = 0

            # ************************************************************
            # ***** ROUTING                               ****************
            # ************************************************************
            if option['InitLisflood']: self.var.IsChannelKinematic = self.var.IsChannel.copy()
            # use kinematic routing in all grid cells

            # KINEMATIC ROUTING - InitLisflood
            if option['InitLisflood'] or (not(option['SplitRouting']) and (not(option['MCTRouting']))):
                #cmcheck no need to copy into another variable, use self.var.ChanQKin
                # Kinematic routing
                ChanQKinOutStart = self.var.ChanQKin.copy()
                # Outflow (x+dx) at time t beginning of calculation step (instant)
                # This is used to calculate inflow from upstream cells

                ChanM3KinStart = self.var.ChanM3Kin.copy()
                # Channel storage at time t beginning of calculation step (instant)

                ########
                ChanQKinOutEnd,ChanM3KinEnd = self.KINRouting(ChanQKinOutStart,SideflowChan)
                # Outflow (x+dx) at time t+dt end of calculation step (instant)
                # Channel storage at time t+dt end of calculation step (instant)
                ########

                # updating variables for next step
                self.var.ChanQKin = ChanQKinOutEnd.copy()
                # Outflow (x+dx) Q at time t+dt (end of calc step) (instant)
                self.var.ChanM3Kin = ChanM3KinEnd.copy()
                # Channel storage V at time t+dt (end of calc step) (instant)

                self.var.ChanQ = ChanQKinOutEnd.copy()
                # Outflow (x+dx) Q at the end of computation step t+dt for full section (instant)
                # same as ChanQKinOutEnd for Kinematic routing only
                self.var.ChanM3 = ChanM3KinEnd.copy()
                # Channel storage V at the end of computation step t+dt for full section (instant)
                # same as ChanM3KinEnd for Kinematic routing only

                self.var.sumDisDay += self.var.ChanQ
                # sum of total river outflow on model sub-step


            # SPLIT ROUTING - no InitLisfllod
            if not option['InitLisflood'] and option['SplitRouting'] and not(option['MCTRouting']):
                self.SplitRouting(SideflowChan)

                # --- Combine the two lines of routing together ---
                #cmcheck moved here from dynamic
                self.var.ChanQ = np.maximum(self.var.ChanQKin + self.var.Chan2QKin - self.var.QLimit, 0)
                # (real) total outflow (at x + dx) at time t + dt (instant)
                # Superposition Kinematic
                # Main channel routing and floodplains routing
                self.var.ChanM3 = self.var.ChanM3Kin + self.var.Chan2M3Kin - self.var.Chan2M3Start
                # Total channel storage [m3] = Volume in main channel (ChanM3Kin) + volume above bankfull (Chan2M3Kin - Chan2M3Start)
                # at t+dt (instant)

                self.var.sumDisDay += self.var.ChanQ
                # sum of total river outflow on model sub-step


            # KINEMATIC ROUTING AND MUSKINGUM-CUNGE-TODINI - no InitLisflood
            if not option['InitLisflood'] and (not(option['SplitRouting']) and (option['MCTRouting'])):
                ####################
                # Kinematic routing
                # Solving Kinematic routing first
                # Kinematic routing is solved on all pixels (including MCT pixels) because we need input from upstream Kin pixels to MCT
                # cmcheck this should be changed because it's a waste of computation time

                ChanQKinOutStart = self.var.ChanQ.copy()
                # Outflow (x+dx) Q at time t beginning of calculation step (instant)

                # ChanM3KinStart = self.var.ChanM3.copy()
                # Channel storage at time t (instant)

                ChanQKinOutEnd,ChanM3KinEnd = self.KINRouting(ChanQKinOutStart,SideflowChan)
                # Outflow at time t+dt end of calculation step (instant)
                # Channel storage at time t+dt end of calculation step (instant)


                ####################
                # MCT routing

                ChanQMCTOutStart = self.var.ChanQ.copy()
                # Outflow (x+dx) at time t  q10 (instant)

                ######cm
                # ChanQKinOutEnd[ChanQKinOutEnd != 0] = 0     #cmcheck metto a 1 la portata in arrivo dalle celle Kinematic
                ######cm

                ChanM3Start = self.var.ChanM3.copy()
                # Channel storage at time t V0

                ChanQMCTInStart = self.var.PrevQMCTin.copy()
                # Inflow (x) at time t
                # This is coming from upstream pixels

                # calling MCT routing
                # using ChanQKinOutEnd from Kinematic routing to have inflow from upstream kinematic pixels
                ChanQMCTOutEnd,ChanM3MCTEnd,Cmend, Dmend = self.MCTRoutingLoop(ChanQMCTOutStart,ChanQMCTInStart,ChanQKinOutEnd,SideflowChanMCT,ChanM3Start)
                # Outflow (x+dx) at time t+dt end of calculation step (instant)
                # Channel storage at time t+dt end of calculation step (instant)

                # update input (x) Q at t for next step
                ChanQMCTStartPcr = decompress(ChanQMCTOutStart)  # pcr
                self.var.PrevQMCTin = compressArray(upstream(self.var.LddChan, ChanQMCTStartPcr))
                # using LddChan here because we need to input from upstream pixels to include kinematic pixels

                # Storing MCT Courant and Reynolds numbers for state files
                self.var.PrevCm0 = Cmend
                self.var.prevDm0 = Dmend

                ####################
                # combine results from Kinematic and MCT pixels at x+dx t+dt (instant)
                self.var.ChanQ = np.where(self.var.IsChannelKinematic, ChanQKinOutEnd, ChanQMCTOutEnd)
                # Outflow (x+dx) Q at the end of computation step t+dt (instant)
                self.var.ChanM3 = np.where(self.var.IsChannelKinematic, ChanM3KinEnd, ChanM3MCTEnd)
                # Channel storage V at the end of computation step t+dt (instant)
                # ChanQOutAvg = np.where(self.var.IsChannelKinematic, ChanQKinOutAvg, ChanQMCTOutAvg)
                # Average outflow Q over the calculation step (average)


                self.var.sumDisDay += self.var.ChanQ
                # sum of total river outflow on model sub-step


            TotalCrossSectionArea = np.maximum(self.var.ChanM3Kin * self.var.InvChanLength, 0.01)

            ###
            
            # ---- Uncomment lines 603-635 in order to compute the mass balance error within the routing module for the options (i) initial run or (ii) split routing off ----
            #'''
            # option['repMBTs']=True
            if option['repMBTs']:
                 if option['InitLisflood'] or (not(option['SplitRouting'])):
                    # Kinematic routing and MCT
                    if NoRoutingExecuted == (self.var.NoRoutSteps-1):
                      # StorageStep = self.var.ChanM3Kin.copy()
                      StorageStep = self.var.ChanM3.copy()
                      # Water storage at t+dt end of routing step: rivers channels
                      # cmcheck using ChanM3 so it's OK for both MCT and KIN

                      ChanQAvgR = self.var.sumDisDay/self.var.NoRoutSteps
                      # average (of instantaneous) outflow (x+dx) at t+dt end of routing step
                      sum1=ChanQAvgR.copy()
                      sum1[self.var.AtLastPointC == 0] = 0
                      OutStep = np.take(np.bincount(self.var.Catchments,weights=sum1 * self.var.DtSec),self.var.Catchments)
                      # average outflow volume (x+dx) volume at t+dt

                      maskinfo = MaskInfo.instance()
                      DisStructureR = maskinfo.in_zero()
                      DischargeM3StructuresR = maskinfo.in_zero()

                      if not option['InitLisflood']:
                       if option['simulateReservoirs']:
                         sum1 =self.var.ChanQ.copy()
                         StorageStep =  StorageStep + self.var.ReservoirStorageM3.copy()
                         DisStructureR = np.where(self.var.IsUpsOfStructureKinematicC, sum1 * self.var.DtRouting, 0)
                         DischargeM3StructuresR = np.take(np.bincount(self.var.Catchments, weights=DisStructureR), self.var.Catchments)
                         DischargeM3StructuresR -= self.var.DischargeM3StructuresIni

                      if not option['InitLisflood']:
                       if option['simulateLakes']:
                         sum1 =self.var.ChanQ.copy()
                         StorageStep =  StorageStep + self.var.LakeStorageM3Balance.copy()
                         DisStructureR = np.where(self.var.IsUpsOfStructureKinematicC, sum1 * self.var.DtRouting, 0)
                         DischargeM3StructuresR = np.take(np.bincount(self.var.Catchments, weights=DisStructureR), self.var.Catchments)
                         maskinfo = MaskInfo.instance()
                         DisLake = maskinfo.in_zero()
                         np.put(DisLake, self.var.LakeIndex, 0.5 * self.var.LakeInflowCC * self.var.DtRouting)
                         DischargeM3Lake = np.take(np.bincount(self.var.Catchments, weights=DisLake),self.var.Catchments)
                         DischargeM3StructuresR += DischargeM3Lake
                         DischargeM3StructuresR -= self.var.DischargeM3StructuresIni

                      # Total Mass Balance Error in m3 per catchment for Initial Run OR Kinematic routing (Split Routing OFF)
                      MB =- np.sum(StorageStep)+np.sum(self.var.StorageStepINIT) - OutStep[0]  -DischargeM3StructuresR[0] +self.var.AddedTRUN
                      self.var.StorageStepINIT= np.sum(StorageStep) + DischargeM3StructuresR[0]
            #'''

            # ---- Uncomment lines in order to compute the mass balance error within the routing module for the options split routing  ----
            #'''
            if option['repMBTs']:
                 if (not(option['InitLisflood'])) and (option['SplitRouting']):
                    # compute the mass balance at the last of the sub-routing steps in order to account for the contributions of lakes and reservoirs
                    if NoRoutingExecuted == (self.var.NoRoutSteps-1):
                      ChanQAvgSR = self.var.sumDisDay/self.var.NoRoutSteps  #self.var.ChanQ
                      sum1=ChanQAvgSR.copy()
                      sum1[self.var.AtLastPointC == 0] = 0
                      OutStep = np.take(np.bincount(self.var.Catchments,weights=sum1 * self.var.DtSec),self.var.Catchments)

                      StorageStep=[]
                      StorageStep= self.var.ChanM3Kin.copy()+self.var.Chan2M3Kin.copy()-self.var.Chan2M3Start.copy()


                      maskinfo = MaskInfo.instance()
                      DisStructureSR = maskinfo.in_zero()
                      DischargeM3StructuresR = maskinfo.in_zero()

                      if option['simulateReservoirs']:
                         sum1=[]
                         sum1 =self.var.ChanQ.copy()
                         StorageStep =  StorageStep + self.var.ReservoirStorageM3.copy()
                         DisStructureSR = np.where(self.var.IsUpsOfStructureKinematicC, sum1 * self.var.DtRouting, 0)
                         DischargeM3StructuresR = np.take(np.bincount(self.var.Catchments, weights=DisStructureSR), self.var.Catchments)
                         DischargeM3StructuresR -= self.var.DischargeM3StructuresIni

                      if option['simulateLakes']:
                         sum1 =self.var.ChanQ.copy()
                         StorageStep =  StorageStep + self.var.LakeStorageM3Balance.copy()
                         DisStructureSR = np.where(self.var.IsUpsOfStructureKinematicC, sum1 * self.var.DtRouting, 0)
                         DischargeM3StructuresR = np.take(np.bincount(self.var.Catchments, weights=DisStructureSR), self.var.Catchments)
                         DisLake = maskinfo.in_zero()
                         np.put(DisLake, self.var.LakeIndex, 0.5 * self.var.LakeInflowCC * self.var.DtRouting)
                         DischargeM3Lake = np.take(np.bincount(self.var.Catchments, weights=DisLake),self.var.Catchments)
                         DischargeM3StructuresR += DischargeM3Lake

                         DischargeM3StructuresR -= self.var.DischargeM3StructuresIni

                      # Mass Balance Error due to the Split Routing module
                      StorageStep1=np.take(np.bincount(self.var.Catchments, weights=StorageStep), self.var.Catchments)

                      self.var.MBErrorSplitRoutingM3  = - StorageStep1 + self.var.StorageStepINIT - OutStep  - DischargeM3StructuresR + self.var.AddedTRUN
                      # Discharge error at the outlet pointt [m3/s]
                      QoutCorrection=self.var.MBErrorSplitRoutingM3/self.var.DtRouting
                      QoutCorrection[self.var.AtLastPointC == 0] = 0
                      self.var.OutletDischargeErrorSplitRouting = np.take(np.bincount(self.var.Catchments,weights=QoutCorrection),self.var.Catchments)

                      self.var.StorageStepINIT= StorageStep1.copy()+DischargeM3StructuresR
             #'''


            self.var.FlowVelocity = np.minimum(self.var.ChanQKin/TotalCrossSectionArea, 0.36*self.var.ChanQKin**0.24)
            # Channel velocity (m/s); dividing Q (m3/s) by CrossSectionArea (m2)
            # avoid extreme velocities by using the Wollheim 2006 equation
            # assume 0.1 for upstream areas (outside ChanLdd)
            self.var.FlowVelocity *= np.minimum(np.sqrt(self.var.PixelArea)*self.var.InvChanLength,1)
            # reduction for sinuosity of channels
            self.var.TravelDistance=self.var.FlowVelocity*self.var.DtSec
            # if flow is fast, Traveltime=1, TravelDistance is high: Pixellength*DtSec
            # if flow is slow, Traveltime=DtSec then TravelDistance=PixelLength
            # maximum set to 30km/day for 5km cell, is at DtSec/Traveltime=6, is at Traveltime<DtSec/6


    def KINRouting(self,ChanQKin,SideflowChan):
        """Based on a 4-point implicit finite-difference numerical solution of the kinematic wave equations.
        Given the instantaneous flow rate (discharge), the corresponding amount of water stored in the channel
        is calculated using Manning equation for steady state flow where Alpha is currently fixed
        at half of bankfull depth.
        See: Te Chow, V. and Maidment, D.R. and Mays, L.W. (1988). Applied Hydrology. McGraw-Hill. (Sec. 9.6)
        https://ponce.sdsu.edu/Applied_Hydrology_Chow_1988.pdf
        A = alpha * Q**beta
        V = chanlength * alpha * Q**beta
        Takes:
        ChanQKin = inflow (at x) from upstream channels [m3/sec] (instant)
        SideflowChan = lateral inflow into the channel segment (cell) [m3/channellength/sec]
        :returns
        ChanQKin = outflow (x+dx) at time t+dt (instant) [m3/s]
        ChanM3Kin = amount of water stored in the channel at time t+dt (instant) [m3]
        """

        #  ---- Single Routing ---------------
        # No split routing
        # side flow consists of runoff (incl. groundwater), inflow from reservoirs (optional) and external inflow hydrographs (optional)
        SideflowChan[np.isnan(SideflowChan)] = 0 # TEMPORARY FIX - SEE DEBUG ABOVE!

        # ChanQKinInStartPcr = decompress(ChanQKin)  # pcr
        # ChanQKinInStart = compressArray(upstream(self.var.LddKinematic, ChanQKinInStartPcr))
        # # Inflow (at space x) at time t+dt beginning of calculation step (instant) from upstream cells
        #
        # ChanM3KinStart = self.var.ChanLength * self.var.ChannelAlpha * ChanQKin**self.var.Beta
        # # ChanM3KinStart is the Volume in channel at time t beginning of computation step (instant)

        ####################################################################################################
        #self.river_router.kinematicWaveRouting(self.var.ChanQKin, SideflowChan, "main_channel")
        self.river_router.kinematicWaveRouting(ChanQKin, SideflowChan, "main_channel")
        # ChanQKin is outflow (at x+dx) at time t in input and at time t+dt in output (instant)
        ####################################################################################################

        #self.var.ChanM3Kin = self.var.ChanLength * self.var.ChannelAlpha * self.var.ChanQKin**self.var.Beta
        ChanM3Kin = self.var.ChanLength * self.var.ChannelAlpha * ChanQKin**self.var.Beta
        # ChanM3Kin is the Volume in channel at end of computation step (at t+dt) (instant)

        #self.var.ChanM3Kin = np.maximum(self.var.ChanM3Kin, 0.0)
        ChanM3Kin = np.maximum(ChanM3Kin, 0.0)
        # Check for negative volumes at the end of computation step
        # Volume at time t+dt

        #self.var.ChanQKin = (self.var.ChanM3Kin * self.var.InvChanLength * self.var.InvChannelAlpha) ** (self.var.InvBeta)
        ChanQKin = (ChanM3Kin * self.var.InvChanLength * self.var.InvChannelAlpha) ** (self.var.InvBeta)
        # Correct for negative discharge at the end of computation step (instant)
        # Outflow (x+dx) at time t+dt

        return ChanQKin, ChanM3Kin


    def SplitRouting(self, SideflowChan):
        #  ---- Double Routing ---------------
        # routing is split in two (virtual) channels: main channel and virtual channel representing floodplains

        #Split sideflow between the two lines of routing
        # Ad
        SideflowRatio = np.where((self.var.ChanM3Kin + self.var.Chan2M3Kin) > 0,
                                 self.var.ChanM3Kin / (self.var.ChanM3Kin + self.var.Chan2M3Kin), 0.0)

        # CM ##################################
        # self.var.Sideflow1Chan = np.where(self.var.ChanM3Kin > self.var.M3Limit, SideflowRatio*SideflowChan, SideflowChan)
        # This is creating instability because ChanM3Kin can be < M3Limit between two routing sub-steps
        # TO BY REPLACED WITH THE FOLLOWING
        self.var.Sideflow1Chan = np.where(
            (self.var.ChanM3Kin + self.var.Chan2M3Kin - self.var.Chan2M3Start) > self.var.M3Limit,
            SideflowRatio * SideflowChan, SideflowChan)
        # sideflow to the main channel
        #######################################

        self.var.Sideflow1Chan = np.where(np.abs(SideflowChan) < 1e-7, SideflowChan, self.var.Sideflow1Chan)
        # too small values are avoided
        Sideflow2Chan = SideflowChan - self.var.Sideflow1Chan
        # sideflow to the 'floodplains' channel

        Sideflow2Chan = Sideflow2Chan + self.var.Chan2QStart * self.var.InvChanLength  # original
        #cmcheck should I use Qlimit instead of Chan2QStart ???
        # as kinematic wave gets slower with less water
        # a constant amount of water has to be added
        # -> add QLimit discharge


        # --- Main channel routing ---
        self.river_router.kinematicWaveRouting(self.var.ChanQKin, self.var.Sideflow1Chan, "main_channel")
        # sef.var.ChanQKin is outflow from main channel (at x+dx) at time t in input and at time t+dt in output (instant)
        self.var.ChanM3Kin = self.var.ChanLength * self.var.ChannelAlpha * self.var.ChanQKin ** self.var.Beta
        # self.var.ChanM3Kin is the Volume in main channel at end of computation step (at t+dt) (instant)

        self.var.ChanM3Kin = np.maximum(self.var.ChanM3Kin, 0.0)
        # Check for negative volumes at the end of computation step in main channel
        # Volume in main channel at t+dt
        self.var.ChanQKin = (self.var.ChanM3Kin * self.var.InvChanLength * self.var.InvChannelAlpha) ** (
            self.var.InvBeta)
        # Correct negative discharge at the end of computation step
        # Outflow (x+dx) at t+dt (instant)


        # --- Floodplains channel routing (increased Manning coeff) ---
        self.river_router.kinematicWaveRouting(self.var.Chan2QKin, Sideflow2Chan, "floodplains")
        # sef.var.Chan2QKin is (virtual) total outflow (at x+dx) at time t in input and at time t+dt in output (instant) (using increased Manninig coeff)
        self.var.Chan2M3Kin = self.var.ChanLength * self.var.ChannelAlpha2 * self.var.Chan2QKin ** self.var.Beta
        # self.var.Chan2M3Kin is total (virtual) volume of water in river channel when second routing line is active (= using increased Manning coeff)

        diffM3 = self.var.Chan2M3Kin - self.var.Chan2M3Start
        self.var.Chan2M3Kin = np.where(diffM3 < 0.0, self.var.Chan2M3Start, self.var.Chan2M3Kin)
        # Check for negative volume over bankfull at the end of routing step
        # Total volume cannot be smaller than the bankfull volume calculated with increased Manning coeff (Chan2M3Start)

        self.var.CrossSection2Area = (self.var.Chan2M3Kin - self.var.Chan2M3Start) * self.var.InvChanLength
        # Compute cross-section area for floodplains only in second line of routing (above bankfull)

        self.var.Chan2QKin = (self.var.Chan2M3Kin * self.var.InvChanLength * self.var.InvChannelAlpha2) ** (
            self.var.InvBeta)
        # (virtual) total outflow (at x + dx) at time t + dt (instant)(using increased Manninig coeff)
        # Correct negative discharge at the end of computation step in second line

        FldpQKin = self.var.Chan2QKin - self.var.QLimit
        # Outflow at t+dt from floodplains only (above bankfull)

        # # --- Combine the two lines of routing ---
        # self.var.ChanQ = np.maximum(self.var.ChanQKin + self.var.Chan2QKin - self.var.QLimit, 0)
        # # (real) total outflow (at x + dx) at time t + dt (instant)
        # # Superposition Kinematic
        # # Main channel routing and floodplains routing
        # ----------End splitrouting-------------------------------------------------
        return


    def MCTRoutingLoop(self,ChanQMCTOutStart,ChanQMCTInStart,ChanQKinOut,SideflowChanMCT,ChanM3Start):
        """This function implements Muskingum-Cunge-Todini routing method
        MCT routing is calculated on MCT pixels only but gets inflow from both KIN and MCT upstream pixels.
        Function get_mct_pix is used to compress arrays with all river channel pixels to arrays containing MCT pixels only.
        Function put_mct_pix is used to explode arrays containing MCT pixels only back to arrays containing all rivers pixels.
        References:
            Todini, E. (2007). A mass conservative and water storage consistent variable parameter Muskingum-Cunge approach. Hydrol. Earth Syst. Sci.
            (Chapter 5)
            Reggiani, P., Todini, E., & Meißner, D. (2016). On mass and momentum conservation in the variable-parameter Muskingum method. Journal of Hydrology, 543, 562–576. https://doi.org/10.1016/j.jhydrol.2016.10.030
            (Appendix B)
        """

        # channel geometry - MCT pixels only
        xpix = self.get_mct_pix(self.var.ChanLength)         # dimension along the flow direction  [m]
        s0 = self.get_mct_pix(self.var.ChanGrad)             # river bed slope (tan B)
        Balv = self.get_mct_pix(self.var.ChanBottomWidth)    # width of the riverbed [m]
        ChanSdXdY = self.get_mct_pix(self.var.ChanSdXdY)     # slope dx/dy of riverbed side
        Nalv = self.get_mct_pix(self.var.ChanMan)            # channel mannings coefficient n for the riverbed [s/m1/3]
        ANalv = np.arctan(1/ChanSdXdY)                       # angle of the riverbed side [rad]

        dt = self.var.DtSecChannel                           # computation time step for channel [s]

        # MCT Courant and Reynolds numbers from previous step (MCT pixels)
        Cm0 = self.get_mct_pix(self.var.PrevCm0)
        Dm0 = self.get_mct_pix(self.var.PrevDm0)

        # instant discharge at channel input (I x=0) and channel output (O x=1)
        # at the end of previous calculation step I(t) and O(t) and
        # at the end of current calculation step I(t+1) and O(t+1)

        # Inflow at time t
        # I(t)
        # calc contribution from upstream pixels at time t (dim=all pixels)
        q00 = self.get_mct_pix(ChanQMCTInStart)
        # channel storage at the beginning of the computation step (t)
        ChanM3MCT0 = self.get_mct_pix(ChanM3Start)

        # Outflow at time t
        # O(t)
        # dim=mct pixels
        q10 = self.get_mct_pix(ChanQMCTOutStart)

        # calc contribution from upstream pixels at time t+1 (dim=all pixels because we need to include both MCT and KIN pixels at the same time)
        ChanQMCTPcr=decompress(ChanQKinOut)    #pcr
        ChanQMCTUp1=compressArray(upstream(self.var.LddChan,ChanQMCTPcr))
        # Inflow at time t+1
        # I(t+dt)
        # dim=mct pixels
        q01 = self.get_mct_pix(ChanQMCTUp1)

        # Outflow (x+1) at time t+1
        # O(t+dt)
        # dim=mct pixels
        # set to zero at beginning of computation
        q11 = np.zeros_like(q01)
        # qout_ave = np.zeros_like(q01)
        V11 = np.zeros_like(q01)

        # Lateral flow Ql (average) during interval dt [m3/s]
        # Ql(t)
        # calc contribution from sideflow
        ql = self.get_mct_pix(SideflowChanMCT)


        ### start pixels loop ###
        # Pixels in the same order are independent and can be routed in parallel.
        # Orders must be processed in series, starting from order 0.
        # Outflow from MCT pixels can only go to a MCT pixel
        ChanQOut = ChanQKinOut.copy()
        num_orders = self.mct_river_router.order_start_stop.shape[0]
        for order in range(num_orders):
            first = self.mct_river_router.order_start_stop[order, 0]
            last = self.mct_river_router.order_start_stop[order, 1]
            for index in range(first, last):
                # get pixel ID
                idpix = self.mct_river_router.pixels_ordered[index]

                ### calling MCT function for single cell
                q11[idpix], V11[idpix], Cm0[idpix], Dm0[idpix] = self.MCTRouting_single(q10[idpix], q01[idpix], q00[idpix], ql[idpix], Cm0[idpix], Dm0[idpix],
                                                                                                         dt, xpix[idpix], s0[idpix], Balv[idpix], ANalv[idpix], Nalv[idpix])
                # q11[idpix] = q01[idpix]     # tanto entra tanto esce nelle celle mct

            # Update contribution from upstream pixels at time t+1 (dim=all pixels) using the newly calculated q11
            # I want to update q01 (inflow at t+1) for cells downstream of idpix using the newly calculated q11
            Q11 = self.put_mct_pix(q11)

            # combine results in pixels of this order with results in pixels of upstream orders
            ChanQOut = np.where(Q11 == 0, ChanQOut, Q11)

            # for each pixel in the catchment, calc contribution from upstream pixels
            QupPcr=decompress(ChanQOut)    #pcr
            Qup01=compressArray(upstream(self.var.LddChan,QupPcr))

            # slice the MCT pixels
            # Inflow at time t+1
            # I(t+dt)
            # dim=mct pixels
            q01 = self.get_mct_pix(Qup01)

            # repeat for next order of pixels

        ### end pixels loop ###

        # explode arrays with MCT results on all catchment pixels
        ChanQMCTOut = self.put_mct_pix(q11)
        Cmout = self.put_mct_pix(Cm0)
        Dmout = self.put_mct_pix(Dm0)
        ChanM3MCTOut = self.put_mct_pix(V11)
        #ChanQMCTOutAve = self.put_mct_pix(qout_ave)

        return ChanQMCTOut, ChanM3MCTOut, Cmout, Dmout


    def MCTRouting_single(self, q10, q01, q00, ql, Cm0, Dm0, dt, xpix, s0, Balv, ANalv, Nalv):
        '''
        This function implements Muskingum-Cunge-Todini routing method for a single channel pixel.
        References:
            Todini, E. (2007). A mass conservative and water storage consistent variable parameter Muskingum-Cunge approach. Hydrol. Earth Syst. Sci.
            (Chapter 5)
            Reggiani, P., Todini, E., & Meißner, D. (2016). On mass and momentum conservation in the variable-parameter Muskingum method. Journal of Hydrology, 543, 562–576. https://doi.org/10.1016/j.jhydrol.2016.10.030
            (Appendix B)

        :param q10: O(t) - outflow (x+dx) at time t
        :param q01: I(t+dt) - inflow (x) at time t+dt
        :param q00: I(t) - inflow (x) at time t
        :param ql: lateral flow over time dt [m3/s]
        :param Cm0: Courant number at time t
        :param Dm0: Reynolds number at time t
        :param dt: time interval step
        :param xpix: channel length
        :param s0: channel slope
        :param Balv: channel bankfull width
        :param ANalv: angle of the riverbed side [rad]
        :param Nalv: channel Manning roughness coefficient
        :return:
        q11: Outflow (x+dx) at O(t+dt)
        V11: channel storage volume at t+dt
        Cm1: Courant number at t+1 for state file
        Dm1: Reynolds number at t+1 for state file
        '''

        eps = 1e-06

        # Calc O' first guess for the outflow at time t+dt
        # O'(t+dt)=O(t)+(I(t+dt)-I(t))
        q11 = q10 + (q01 - q00)
        # check for negative discharge values
        if q11 < 0:
            q11 = 0.

        # calc reference discharge at time t
        # qm0 = (I(t)+O(t))/2
        # qm0 = (q00 + q10) / 2.

        # Calc O(t+dt)=q11 at time t+dt using MCT equations
        for i in range(2):  # repeat 2 times for accuracy

            # reference I discharge at x=0
            qmx0 = (q00 + q01) / 2.
            if qmx0 == 0:
                qmx0 = eps
            hmx0 = self.hoq(qmx0, s0, Balv, ANalv, Nalv)

            # reference O discharge at x=1
            qmx1 = (q10 + q11) / 2.
            if qmx1 == 0:
                qmx1 = eps
            hmx1 = self.hoq(qmx1, s0,Balv,ANalv,Nalv)

            # Calc riverbed slope correction factor
            cor = 1 - (1 / s0 * (hmx1 - hmx0) / xpix)
            sfx = s0 * cor
            if sfx < (0.8 * s0):
                sfx = 0.8 * s0   # In case of instability raise from 0.5 to 0.8

            # Calc reference discharge time t+dt
            # Q(t+dt)=(I(t+dt)+O'(t+dt))/2
            qm1 = (q01 + q11) / 2.
            #cm
            if qm1 == 0:
                qm1 = eps
            #cm
            hm1 = self.hoq(qm1,s0,Balv,ANalv,Nalv)
            dummy, Ax1,Bx1,Px1,ck1 = self.qoh(hm1,s0,Balv,ANalv,Nalv)
            if (ck1 <= eps):
                ck1 = eps

            # Calc correcting factor Beta at time t+dt
            Beta1 = ck1 / (qm1 / Ax1)
            # calc corrected cell Reynolds number at time t+dt
            Dm1 = qm1 / (sfx * ck1 * Bx1 * xpix) / Beta1
            # corrected Courant number at time t+dt
            Cm1 = ck1 * dt / xpix / Beta1

            # Calc MCT parameters
            den = 1 + Cm1 + Dm1
            c1 = (-1 + Cm1 + Dm1) / den
            c2 = (1 + Cm0 - Dm0) / den * (Cm1 / Cm0)
            c3 = (1 - Cm0 + Dm0) / den * (Cm1 / Cm0)
            c4 = (2 * Cm1) / den

            # Calc outflow q11 at time t+1
            # Mass balance equation without lateral flow
            # q11 = c1 * q01 + c2 * q00 + c3 * q10
            # Mass balance equation that takes into consideration the lateral flow
            q11 = c1 * q01 + c2 * q00 + c3 * q10 + c4 * ql

            if q11 < 0.:
                q11 = eps

            #### end of for loop

        k1 = dt / Cm1
        x1 = (1. - Dm1) / 2.

        # Calc the corrected mass-conservative expression for the reach segment storage at time t+dt
        # The lateral inflow ql is only explicitly accounted for in the mass balance equation, while it is not in the equation expressing
        # the storage as a weighted average of inflow and outflow.The rationale of this approach lies in the fact that the outflow
        # of the reach implicitly takes the  effect of the lateral inflow into account.
        V11 = (1-Dm1)*dt/(2*Cm1)*q01 + (1+Dm1)*dt/(2*Cm1)*q11

        if (V11 < 0):
            V11=0.

        # Outflow at O(t+dt), average outflow in time dt, water volume at t+dt, Courant and Reynolds numbers at t+1 for state files
        return q11, V11, Cm1, Dm1


    def hoq(self,q,s0,Balv,ANalv,Nalv):
        """Water depth from discharge.
        Given a generic cross-section (rectangular, triangular or trapezoidal) and a steady-state discharge q=Q*, it computes
        water depth (y), wet contour (Bx), wet area (Ax) and wave celerity (cel) using Newton-Raphson method.
        Reference:
        Reggiani, P., Todini, E., & Meißner, D. (2016). On mass and momentum conservation in the variable-parameter Muskingum method.
        Journal of Hydrology, 543, 562–576. https://doi.org/10.1016/j.jhydrol.2016.10.030

        Parameters:
        q: steady-state discharge river discharge [m3/s]
        s0: river bed slope (tan B)
        Balv : width of the riverbed [m]
        ChanSdXdY : slope dx/dy of riverbed side
        ANalv : angle of the riverbed side [rad]
        Nalv : channel mannings coefficient n for the riverbed [s/m1/3]

        :returns
        y: water depth referred to the bottom of the riverbed [m]
        """

        alpha = 5./3.     # exponent (5/3)
        eps = 1.e-06
        max_tries = 1000

        rs0 = np.sqrt(s0)
        usalpha = 1. / alpha

        # cotangent(angle of the riverbed side - dXdY)
        if ANalv < np.pi/2:
            # triangular or trapezoid cross-section
            c = self.cotan(ANalv)
        else:
            # rectangular corss-section
            c = 0.

        # sin(angle of the riverbed side - dXdY)
        if ANalv < np.pi/2:
            # triangular or trapezoid cross-section
            s = np.sin(ANalv)
        else:
            # rectangular cross-section
            s = 1.

        # water depth first approximation y0 based on steady state q
        if Balv == 0:
            # triangular cross-section
            y = (Nalv * q / rs0)**(3. / 8.) * (2 / s)**.25 / c**(5. / 8.)
        else:
            # rectangular cross-section and first approx for trapezoidal cross-section
            y = (Nalv * q / (rs0 * Balv))**usalpha

        if (Balv != 0) and (ANalv < np.pi/2):
            # trapezoid cross-section
            y = (Nalv * q / rs0) ** usalpha * (Balv + 2. * y / s) ** .4 / (Balv + c * y)

        for tries in range(1,max_tries):
            # calc Q(y) for the different tries of y
            q0,Ax,Bx,Px,cel = self.qoh(y,s0,Balv,ANalv,Nalv)
            # Ax: wet area[m2]
            # Bx: cross-section width at water surface[m]
            # Px: cross-section wet contour [m]
            # cel: wave celerity[m/s]

            # this is the function we want to find the 0 for f(y)=Q(y)-Q*
            fy = q0 - q
            # calc first derivative of f(y)  f'(y)=Bx(y)*cel(y)
            dfy = Bx * cel
            # calc update for water depth y
            dy = fy / dfy
            # update yt+1=yt-f'(yt)/f(yt)
            y = y - dy
            # stop loop if correction becomes too small
            if np.abs(dy) < eps: break

        return y


    def qoh(self,y,s0,Balv,ANalv,Nalv):
        """ Discharge from water depth.
        Given a generic river cross-section (rectangular, triangular and trapezoidal)
        and a water depth (y [m]) referred to the bottom of the riverbed, it uses Manning’s formula to calculate:
        q: steady-state discharge river discharge [m3/s]
        a: wet area [m2]
        b: cross-section width at water surface [m]
        p: cross-section wet contour [m]
        cel: wave celerity [m/s]

        Parameters:
        y: river water depth [m]
        s0: river bed slope (tan B)
        Balv : width of the riverbed [m]
        ChanSdXdY : slope dx/dy of riverbed side
        ANalv : angle of the riverbed side [rad]
        Nalv : channel mannings coefficient n for the riverbed [s/m1/3]

        Reference: Reggiani, P., Todini, E., & Meißner, D. (2016). On mass and momentum conservation in the variable-parameter Muskingum method. Journal of Hydrology, 543, 562–576. https://doi.org/10.1016/j.jhydrol.2016.10.030

        :return:
        q,a,b,p,cel
        """

        alpha = 5./3.  # exponent (5/3)

        rs0 = np.sqrt(s0)
        alpham = alpha - 1.

        # np.where(ANalv < np.pi/2, triang. or trapeiz., rectangular)
        # cotangent(angle of the riverbed side - dXdY)
        c = np.where(ANalv < np.pi/2,
                     # triangular or trapezoid cross-section
                     self.cotan(ANalv),
                     # rectangular cross-section
                     0.)
        # sin(angle of the riverbed side - dXdY)
        s = np.where(ANalv < np.pi/2,
                     # triangular or trapezoid cross-section
                     np.sin(ANalv),
                     # rectangular corss-section
                     1.)

        a = (Balv + y * c) * y  # wet area [m2]
        b = Balv + 2. * y * c   # cross-section width at water surface [m]
        p = Balv + 2. * y / s   # cross-section wet contour [m]
        q = rs0 / Nalv * a**alpha / p**alpham       # steady-state discharge [m3/s]
        cel = (q / 3.) * (5. / a - 4. / (p * b * s))    # wave celerity [m/s]

        return q,a,b,p,cel


    def hoV(self,V,xpix,Balv,ANalv):
        """Water depth from volume.
        Given a generic river cross-section (rectangular, triangular and trapezoidal) and a volume V,
        it calculates the water depth referred to the bottom of the riverbed [m] (y).
        Takes:
        V : volume of water in channel riverbed
        xpix : dimension along the flow direction  [m]
        Balv : width of the riverbed [m]
        ANalv : angle of the riverbed side [rad]

        Reference: Reggiani, P., Todini, E., & Meißner, D. (2016). On mass and momentum conservation in the variable-parameter Muskingum method. Journal of Hydrology, 543, 562–576. https://doi.org/10.1016/j.jhydrol.2016.10.030
        :return:
        y : channel water depth [m]
        """


        eps = 1e-6

        c = np.where(ANalv < np.pi/2,       # angle of the riverbed side dXdY [rad]
                     self.cotan(ANalv),     # triangular or trapezoidal cross-section
                     0.)                    # rectangular cross-section

        a = V / xpix    # wet area [m2]

        # np.where(c < 1.d-6, rectangular, triangular or trapezoidal)
        y = np.where(np.abs(c) < eps,
                     a/Balv,                                                # rectangular cross-section
                     (-Balv + np.sqrt(Balv**2 + 4 * a * c)) / (2 * c))    # triangular or trapezoidal cross-section

        return y


    def qoV(self,V,xpix,s0,Balv,ANalv,Nalv):
        """ Discharge from volume.
        Given a generic river cross-section (rectangular, triangular and trapezoidal)
        and a water volume (V [m3]), it uses Manning’s formula to calculate the corresponding discharge (q [m3/s]).
        """
        y = self.hoV(V,xpix,Balv,ANalv)
        q, a, b, p, cel = self.qoh(y,s0,Balv,ANalv,Nalv)
        return q


    def cotan(self,x):
        """There is no cotangent function in numpy"""
        return np.cos(x) / np.sin(x)


    def get_mct_pix(self,var):
        """Compress to mct array.
        For any array (var) with all catchment pixels, it masks the MCT pixels (x) and
        reduces the dimension of the array (y).
        :return:
        y: same as input array (var) but only MCT pixels
        """
        x = np.ma.masked_where(self.var.IsChannelKinematic,var)
        y = x.compressed()

        return y


    def put_mct_pix(self,var):
        """Explode mct array.
        For any array (var) with MCT pixels only, it explodes the dimension to all catchment pixels and puts
        values from array var in the corresponting MCT pixels.
        Uses self.var.IsChannelKinematic to define MCT pixels
        :return:
        y: same as input array (var) but only all pixels
        """
        zeros_array = np.zeros(self.var.IsChannelKinematic.shape)
        x = np.ma.masked_where(self.var.IsChannelKinematic, zeros_array)
        x[~x.mask] = var
        # explode results on the MCT pixels mask (dim=all)
        y = x.data
        # update results in array (dim=all)
        return y


    def rad_from_dxdy(self,dxdy):
        """Calculate radians"""
        rad = np.arctan(1 / dxdy)
        angle = np.rad2deg(rad)
        return rad



from .kinematic_wave_parallel import rebuildFlowMatrix, decodeFlowMatrix, streamLookups, topoDistFromSea
import pandas as pd
class mctWave:
    """Build pixels loop for MCT channels"""

    def __init__(self, compressed_encoded_ldd, land_mask):
        """"""

        # Process flow direction matrix: downstream and upstream lookups, and routing orders
        flow_dir = decodeFlowMatrix(rebuildFlowMatrix(compressed_encoded_ldd, land_mask))
        self.downstream_lookup, self.upstream_lookup = streamLookups(flow_dir, land_mask)
        self.num_upstream_pixels = (self.upstream_lookup != -1).sum(1).astype(int) # astype for cython import in windows (to avoid 'long long' buffer dtype mismatch)
        # Routing order: decompose domain into batches; within each batch, pixels can be routed in parallel
        self._setMCTRoutingOrders()

    def _setMCTRoutingOrders(self):
        """Compute the MCT wave routing order. Pixels are grouped in sets with the same order.
        Pixels in the same set are independent and can be routed in parallel. Sets must be processed in series, starting from order 0.
        Pixels are ordered topologically starting from the outlets, as in:
        Liu et al. (2014), A layered approach to parallel computing for spatially distributed hydrological modeling,
        Environmental Modelling & Software 51, 221-227.
        Order MAX is given to pixels with no downstream relations (outlets); order MAX-1 is given to
        pixels whose downstream pixels are all of order MAX; and so on."""
        ocean_topo_distance = topoDistFromSea(self.downstream_lookup, self.upstream_lookup)
        routing_order = ocean_topo_distance.max() - ocean_topo_distance
        self.pixels_ordered = pd.DataFrame({"pixels": np.arange(routing_order.size), "order": routing_order})
        try:
            self.pixels_ordered = self.pixels_ordered.sort_values(["order", "pixels"]).set_index("order").squeeze()
        except: # FOR COMPATIBILITY WITH OLDER PANDAS VERSIONS
            self.pixels_ordered = self.pixels_ordered.sort(["order", "pixels"]).set_index("order").squeeze()
        # Output of pd.DataFrame.squeeze() is not a DataFrame and not a Series.
        if not isinstance(self.pixels_ordered, pd.Series):
            # self.pixels_ordered = pd.DataFrame({'order': [0], 'pixel': self.pixels_ordered})
            # self.pixels_ordered.set_index('order', inplace=True)
            self.pixels_ordered = pd.Series(self.pixels_ordered)
            self.pixels_ordered.rename_axis("order")
        order_counts = self.pixels_ordered.groupby(self.pixels_ordered.index).count()
        stop = order_counts.cumsum()
        self.order_start_stop = np.column_stack((np.append(0, stop[:-1]), stop)).astype(int) # astype for cython import in windows (see above)
        self.pixels_ordered = self.pixels_ordered.values.astype(int) # astype for cython import in windows (see above)

