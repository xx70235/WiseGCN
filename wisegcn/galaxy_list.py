import healpy as hp
import numpy as np
from scipy.special import gammaincinv
from scipy.special import gammaincc
from configparser import ConfigParser
from wisegcn.email_alert import send_mail
from wisegcn import magnitudes as mag
from wisegcn import mysql_update
import logging
from astropy import units as u
from astropy.coordinates import Angle

# settings:
config = ConfigParser(inline_comment_prefixes=';')
config.read('config.ini')
cat_file = config.get('CATALOG', 'PATH')+config.get('CATALOG', 'NAME')+'.npy'  # galaxy catalog file

# parameters:
credzone = config.getfloat('GALAXIES', 'CREDZONE')
relaxed_credzone = config.getfloat('GALAXIES', 'RELAXED_CREDZONE')
nsigmas_in_d = config.getfloat('GALAXIES', 'NSIGMAS_IN_D')
relaxed_nsigmas_in_d = config.getfloat('GALAXIES', 'RELAXED_NSIGMAS_IN_D')
completenessp = config.getfloat('GALAXIES', 'COMPLETENESS')
min_galaxies = config.getfloat('GALAXIES', 'MINGALAXIES')  # minimal number of galaxies
max_galaxies = config.getint('GALAXIES', 'MAXGALAXIES')  # maximal number of galaxies to use

# magnitude of event in r-band. values are value from Barnes... +-1.5 mag
minmag = config.getfloat('GALAXIES', 'MINMAG')
maxmag = config.getfloat('GALAXIES', 'MAXMAG')
sensitivity = config.getfloat('GALAXIES', 'SENSITIVITY')

min_dist_factor = config.getfloat('GALAXIES', 'MINDISTFACTOR')  # reflecting a small chance that the theory is completely wrong and we can still see something

minL = mag.f_nu_from_magAB(minmag)
maxL = mag.f_nu_from_magAB(maxmag)

# Schechter function parameters:
alpha = config.getfloat('GALAXIES', 'ALPHA')
MB_star = config.getfloat('GALAXIES', 'MB_STAR')  # random slide from https://www.astro.umd.edu/~richard/ASTRO620/LumFunction-pp.pdf but not really...?


def find_galaxy_list(skymap_path, completeness = completenessp, credzone = 0.99, relaxed_credzone = 0.99995):
    # Read the HEALPix sky map:
    try:
        prob, dist_mu, dist_sigma, dist_norm = hp.read_map(skymap_path, field=None, verbose=False)
    except Exception as e:
        logging.error('Failed to read sky map!')
        send_mail(subject="[GW@Wise] Failed to read LVC sky map",
                  text='''FITS file: {}
                          Exception: {}'''.format(skymap_path, e))

    # Load the galaxy catalog (glade_id, RA, DEC, distance, Bmag):
    galaxy_cat = np.load(cat_file)
    galaxy_cat = (galaxy_cat[np.where(galaxy_cat[:, 3] > 0), :])[0]  # remove entries with a negative distance
    cat_id = galaxy_cat[:, 0]
    cat_ra = galaxy_cat[:, 1]
    cat_dec = galaxy_cat[:, 2]
    cat_dist = galaxy_cat[:, 3]
    cat_Bmag = galaxy_cat[:, 4]

    # Skymap parameters:
    npix = len(prob)
    nside = hp.npix2nside(npix)

    # Convert galaxy WCS (RA, DEC) to spherical coordinates (theta, phi):
    theta = 0.5 * np.pi - np.deg2rad(cat_dec)
    phi = np.deg2rad(cat_ra)
    d = np.array(cat_dist)

    # Convert galaxy coordinates to skymap pixels:
    galaxy_pix = hp.ang2pix(nside, theta, phi)

    # Most probable sky location
    theta_maxprob, phi_maxprob = hp.pix2ang(nside, np.argmax(prob))
    ra_maxprob = np.rad2deg(phi_maxprob)
    dec_maxprob = np.rad2deg(0.5*np.pi - theta_maxprob)

    # Convert to coordinates
    ra_maxprob = Angle(ra_maxprob * u.deg)
    dec_maxprob = Angle(dec_maxprob * u.deg)

    # Find given percent probability zone (default is 99%):
    prob_cutoff = 1
    prob_sum = 0
    npix_credzone = 0

    prob_sorted = np.sort(prob)
    while prob_sum < credzone:
        prob_sum = prob_sum + prob_sorted[-1]
        prob_cutoff = prob_sorted[-1]
        prob_sorted = prob_sorted[:-1]
        npix_credzone = npix_credzone + 1

    # area = npix_credzone * hp.nside2pixarea(nside, degrees=True)

    ####################################################

    # calculate probability for galaxies by the localization map:
    p = prob[galaxy_pix]
    p_dist = dist_norm[galaxy_pix] * np.exp(-(d - dist_mu[galaxy_pix])**2 / (2*dist_sigma[galaxy_pix]**2))

    # cutoffs - 99% of probability by angles and 3sigma by distance:
    within_dist_idx = np.where(np.abs(d - dist_mu[galaxy_pix]) < nsigmas_in_d*dist_sigma[galaxy_pix])
    within_credzone_idx = np.where(p >= prob_cutoff)
    within_idx = np.intersect1d(within_credzone_idx, within_dist_idx)

    do_mass_cutoff = True

    # Relax credzone limits if no galaxies are found:
    if within_idx.size == 0:
        while prob_sum < relaxed_credzone:
            if prob_sorted.size == 0:
                break
            prob_sum = prob_sum + prob_sorted[-1]
            prob_cutoff = prob_sorted[-1]
            prob_sorted = prob_sorted[:-1]
            npix_credzone = npix_credzone + 1
        within_dist_idx = np.where(np.abs(d - dist_mu[galaxy_pix]) < relaxed_nsigmas_in_d * dist_sigma[galaxy_pix])
        within_credzone_idx = np.where(p >= prob_cutoff)
        within_idx = np.intersect1d(within_credzone_idx, within_dist_idx)
        do_mass_cutoff = False

    p = p[within_idx]
    p = (p * (p_dist[within_idx]))  # d**2?

    cat_id = cat_id[within_idx]
    cat_ra = cat_ra[within_idx]
    cat_dec = cat_dec[within_idx]
    cat_dist = cat_dist[within_idx]
    cat_Bmag = cat_Bmag[within_idx]

    if cat_id.size == 0:
        logging.warning("No galaxies in field!")
        logging.warning("99.995% of probability is ", npix_credzone*hp.nside2pixarea(nside, degrees=True), "deg^2")
        logging.warning("Peaking at (deg) RA = {}, Dec = {}".format(
            ra_maxprob.to_string(unit=u.hourangle, sep=':', precision=2, pad=True),
            dec_maxprob.to_string(sep=':', precision=2, alwayssign=True, pad=True)))
        return

    # Normalize luminosity to account for mass:
    luminosity = mag.L_nu_from_magAB(cat_Bmag - 5 * np.log10(cat_dist * (10 ** 5)))
    luminosity_norm = luminosity / np.sum(luminosity)
    normalization = np.sum(p * luminosity_norm)
    score = p * luminosity_norm / normalization

    # Take 50% of mass:

    # The area under the Schechter function between L=inf and the brightest galaxy in the field:
    missing_piece = gammaincc(alpha + 2, 10 ** (-(min(cat_Bmag - 5*np.log10(cat_dist*(10**5))) - MB_star) / 2.5))
    # there are no galaxies brighter than this in the field, so don't count that part of the Schechter function

    while do_mass_cutoff:
        MB_max = MB_star + 2.5 * np.log10(gammaincinv(alpha + 2, completeness + missing_piece))

        if (min(cat_Bmag - 5*np.log10(cat_dist*(10**5))) - MB_star) > 0:
            MB_max = 100  # if the brightest galaxy in the field is fainter than the cutoff brightness - don't cut by brightness

        brightest = np.where(cat_Bmag - 5*np.log10(cat_dist*(10**5)) < MB_max)
        # print MB_max
        if len(brightest[0]) < min_galaxies:
            # Not enough galaxies, allowing fainter galaxies
            if completeness >= 0.9:  # Tried hard enough, just take all of them
                completeness = 1  # Just to be consistent
                do_mass_cutoff = False
            else:
                completeness = (completeness + (1. - completeness) / 2)
        else:  # got enough galaxies
            cat_id = cat_id[brightest]
            cat_ra = cat_ra[brightest]
            cat_dec = cat_dec[brightest]
            cat_dist = cat_dist[brightest]
            cat_Bmag = cat_Bmag[brightest]
            p = p[brightest]
            luminosity_norm = luminosity_norm[brightest]
            score = score[brightest]
            do_mass_cutoff = False

    # Account for the distance
    absolute_sensitivity = sensitivity - 5 * np.log10(cat_dist * (10 ** 5))

    absolute_sensitivity_lum = mag.f_nu_from_magAB(absolute_sensitivity)
    distance_factor = np.zeros(cat_id.shape[0])

    distance_factor[:] = ((maxL - absolute_sensitivity_lum) / (maxL - minL))
    distance_factor[min_dist_factor > (maxL - absolute_sensitivity_lum) / (maxL - minL)] = min_dist_factor
    distance_factor[absolute_sensitivity_lum < minL] = 1
    distance_factor[absolute_sensitivity > maxL] = min_dist_factor

    # Sort galaxies by probability
    ranking_idx = np.argsort(p*luminosity_norm*distance_factor)[::-1]

    # # Count galaxies that constitute 50% of the probability (~0.5*0.98)
    # sum = 0
    # galaxies50per = 0
    # sum_seen = 0
    # while sum < 0.5:
    #     if galaxies50per >= len(ranking_idx):
    #         break
    #     sum = sum + (p[ranking_idx[galaxies50per]] * luminosity_norm[ranking_idx[galaxies50per]]) / float(normalization)
    #     sum_seen = sum_seen + (p[ranking_idx[galaxies50per]] * luminosity_norm[ranking_idx[galaxies50per]] * distance_factor[ranking_idx[galaxies50per]]) / float(normalization)
    #     galaxies50per = galaxies50per + 1
    #
    # # Event statistics:
    # # Ngalaxies_50percent = the number of galaxies consisting 50% of probability (including luminosity but not distance factor)
    # # actual_percentage = usually around 50
    # # seen_percentage = if we include the distance factor - how much do the same galaxies worth
    # # 99percent_area = area of map in [deg^2] consisting 99% (using only the map from LIGO)
    # stats = {"Ngalaxies_50percent": galaxies50per, "actual_percentage": sum*100, "seen_percentage": sum_seen, "99percent_area": area}

    # Limit the maximal number of galaxies to use:
    if len(ranking_idx) > max_galaxies:
        n = max_galaxies
    else:
        n = len(ranking_idx)

    # Create sorted galaxy list (glade_id, RA, DEC, distance(Mpc), Bmag, score, distance factor (between 0-1))
    # The score is normalized so that all the galaxies in the field sum to 1 (before applying luminosity cutoff)
    galaxylist = np.ndarray((n, 7))
    for i in range(ranking_idx.shape[0])[:n]:
        ind = ranking_idx[i]
        galaxylist[i, :] = [cat_id[ind], cat_ra[ind], cat_dec[ind], cat_dist[ind], cat_Bmag[ind],
                            score[ind], distance_factor[ind]]

        # Update galaxy table in SQL database:
        lvc_galaxy_dict = {'voeventid': '(SELECT MAX(id) from voevent_lvc)',
                           'score': score[ind],
                           'gladeid': cat_id[ind]}
        mysql_update.insert_values('lvc_galaxies', lvc_galaxy_dict)

    return galaxylist, ra_maxprob, dec_maxprob
