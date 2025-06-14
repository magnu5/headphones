#  This file is part of Headphones.
#
#  Headphones is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  Headphones is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with Headphones.  If not, see <http://www.gnu.org/licenses/>.

# NZBGet support added by CurlyMo <curlymoo1@gmail.com> as a part of XBian - XBMC on the Raspberry Pi

import os
import re
import string
import random
import urllib.request, urllib.parse, urllib.error
import datetime
import subprocess
import unicodedata
import urllib.parse
from base64 import b16encode, b32decode
from hashlib import sha1

from bencode import encode as bencode
from bencode import decode as bdecode
from pygazelle import api as gazelleapi
from pygazelle import encoding as gazelleencoding
from pygazelle import format as gazelleformat
from pygazelle import release_type as gazellerelease_type
from unidecode import unidecode

import headphones
from headphones.common import USER_AGENT
from headphones.helpers import (
    bytes_to_mb,
    has_token,
    piratesize,
    replace_all,
    replace_illegal_chars,
    sab_replace_dots,
    sab_replace_spaces,
    sab_sanitize_foldername,
    split_string
    )
from headphones.types import Result
from headphones import logger, db, classes, sab, nzbget, request
from headphones import (
    bandcamp,
    deluge,
    notifiers,
    qbittorrent,
    rutracker,
    soulseek,
    transmission,
    utorrent
    )

# Magnet to torrent services, for Black hole. Stolen from CouchPotato.
TORRENT_TO_MAGNET_SERVICES = [
    'https://itorrents.org/torrent/%s.torrent',
    'https://cache.torrentgalaxy.org/get/%s',
    'https://www.seedpeer.me/torrent/%s'
]

ruobj = None
# Persistent Orpheus.network and RED API objects
gazelleobjs = {}


def fix_url(s, charset="utf-8"):
    """
    Fix the URL so it is proper formatted and encoded.
    """

    scheme, netloc, path, qs, anchor = urllib.parse.urlsplit(s)
    path = urllib.parse.quote(path, '/%')
    qs = urllib.parse.quote_plus(qs, ':&=')

    return urllib.parse.urlunsplit((scheme, netloc, path, qs, anchor))


def torrent_to_file(target_file, data):
    """
    Write torrent data to file, and change permissions accordingly. Will return
    None in case of a write error. If changing permissions fails, it will
    continue anyway.
    """

    # Write data to file
    try:
        with open(target_file, "wb") as fp:
            fp.write(data)
    except IOError as e:
        logger.error(
            f"Could not write `{target_file}`: {str(e)}"
        )
        return

    # Try to change permissions
    if headphones.CONFIG.FILE_PERMISSIONS_ENABLED:
        try:
            os.chmod(target_file, int(headphones.CONFIG.FILE_PERMISSIONS, 8))
        except OSError as e:
            logger.warn(f"Could not change permissions for `{target_file}`: {e}")
    else:
        logger.debug(
            f"Not changing file permissions for `{target_file}, since it is disabled")

    # Done
    return True


def read_torrent_name(torrent_file, default_name=None):
    """
    Read the torrent file and return the torrent name. If the torrent name
    cannot be determined, it will return the `default_name`.
    """

    # Open file
    try:
        with open(torrent_file, "rb") as fp:
            torrent_info = bdecode(fp.read())
    except IOError as e:
        logger.error("Unable to open torrent file: %s", torrent_file)
        return

    # Read dictionary
    if torrent_info:
        try:
            return torrent_info["info"]["name"]
        except KeyError:
            if default_name:
                logger.warning("Couldn't get name from torrent file: %s. "
                               "Defaulting to '%s'", e, default_name)
            else:
                logger.warning("Couldn't get name from torrent file: %s. No "
                               "default given", e)

    # Return default
    return default_name


def calculate_torrent_hash(link, data=None):
    """
    Calculate the torrent hash from a magnet link or data. Raises a ValueError
    when it cannot create a torrent hash given the input data.
    """

    if link.startswith("magnet:"):
        torrent_hash = re.findall(r"urn:btih:([\w]{32,40})", link)[0]
        if len(torrent_hash) == 32:
            torrent_hash = b16encode(b32decode(torrent_hash)).lower()
    elif data:
        info = bdecode(data)[b"info"]
        torrent_hash = sha1(bencode(info)).hexdigest()
    else:
        raise ValueError("Cannot calculate torrent hash without magnet link "
                         "or data")

    return torrent_hash.upper()


def get_seed_ratio(provider):
    """
    Return the seed ratio for the specified provider if applicable. Defaults to
    None in case of an error.
    """

    if provider == 'rutracker.org':
        seed_ratio = headphones.CONFIG.RUTRACKER_RATIO
    elif provider == 'Orpheus.network':
        seed_ratio = headphones.CONFIG.ORPHEUS_RATIO
    elif provider == 'Redacted':
        seed_ratio = headphones.CONFIG.REDACTED_RATIO
    elif provider == 'The Pirate Bay':
        seed_ratio = headphones.CONFIG.PIRATEBAY_RATIO
    elif provider.startswith("Torznab"):
        host = provider.split('|')[2]
        if host == headphones.CONFIG.TORZNAB_HOST:
            seed_ratio = headphones.CONFIG.TORZNAB_RATIO
        else:
            for torznab in headphones.CONFIG.get_extra_torznabs():
                if host == torznab[0]:
                    seed_ratio = torznab[2]
                    break
    else:
        seed_ratio = None

    if seed_ratio is not None:
        try:
            seed_ratio = float(seed_ratio)
        except ValueError:
            logger.warn("Could not get seed ratio for %s" % provider)

    return seed_ratio


def get_provider_name(provider):
    """
    Return the provider name for the provider
    """

    if provider.startswith("Torznab"):
        provider_name = "Torznab " + provider.split("|")[1]
    elif provider.startswith(("http://", "https://")):
        provider_name = provider.split("//")[1]
    else:
        provider_name = provider

    return provider_name


def pirate_bay_get_magnet(info_hash, torrent_name):
    trackers = [
        "udp://tracker.coppersurfer.tk:6969/announce",
        "udp://9.rarbg.me:2850/announce",
        "udp://9.rarbg.to:2920/announce",
        "udp://tracker.opentrackr.org:1337",
        "udp://tracker.internetwarriors.net:1337/announce",
        "udp://tracker.leechers-paradise.org:6969/announce",
        "udp://tracker.pirateparty.gr:6969/announce",
        "udp://tracker.cyberia.is:6969/announce",
    ]
    trackers = "".join([f"&tr={urllib.parse.quote(t, safe='')}" for t in trackers])
    name = urllib.parse.quote(torrent_name, safe="")
    return f"magnet:?xt=urn:btih:{info_hash}&dn={name}{trackers}"


def searchforalbum(albumid=None, new=False, losslessOnly=False,
                   choose_specific_download=False):
    logger.info('Searching for wanted albums')
    myDB = db.DBConnection()

    if not albumid:
        results = myDB.select(
            'SELECT * from albums WHERE Status="Wanted" OR Status="Wanted Lossless"')

        for album in results:

            if not album['AlbumTitle'] or not album['ArtistName']:
                logger.warn('Skipping release %s. No title available', album['AlbumID'])
                continue

            if headphones.CONFIG.WAIT_UNTIL_RELEASE_DATE and album['ReleaseDate']:
                release_date = strptime_musicbrainz(album['ReleaseDate'])
                if not release_date:
                    logger.warn("No valid date for: %s. Skipping automatic search" %
                                album['AlbumTitle'])
                    continue

                elif release_date > datetime.datetime.today():
                    logger.info("Skipping: %s. Waiting for release date of: %s" % (
                        album['AlbumTitle'], album['ReleaseDate']))
                    continue

            new = True

            if album['Status'] == "Wanted Lossless":
                losslessOnly = True

            logger.info('Searching for "%s - %s" since it is marked as wanted' % (
                album['ArtistName'], album['AlbumTitle']))
            do_sorted_search(album, new, losslessOnly)

    elif albumid and choose_specific_download:
        album = myDB.action('SELECT * from albums WHERE AlbumID=?', [albumid]).fetchone()
        logger.info('Searching for "%s - %s"' % (album['ArtistName'], album['AlbumTitle']))
        results = do_sorted_search(album, new, losslessOnly, choose_specific_download=True)
        return results

    else:
        album = myDB.action('SELECT * from albums WHERE AlbumID=?', [albumid]).fetchone()
        logger.info('Searching for "%s - %s" since it was marked as wanted' % (
            album['ArtistName'], album['AlbumTitle']))
        do_sorted_search(album, new, losslessOnly)

    logger.info('Search for wanted albums complete')


def strptime_musicbrainz(date_str):
    """
    Release date as returned by Musicbrainz may contain the full date (Year-Month-Day)
    but it may as well be just Year-Month or even just the year.

    Args:
        date_str: the date as a string (ex: "2003-05-01", "2003-03", "2003")

    Returns:
        The more accurate datetime object we can create or None if parse failed
    """
    acceptable_formats = ('%Y-%m-%d', '%Y-%m', '%Y')
    for date_format in acceptable_formats:
        try:
            return datetime.datetime.strptime(date_str, date_format)
        except:
            pass
    return None


def do_sorted_search(album, new, losslessOnly, choose_specific_download=False):


    NZB_PROVIDERS = (headphones.CONFIG.HEADPHONES_INDEXER or
                     headphones.CONFIG.NEWZNAB or
                     headphones.CONFIG.NZBSORG or
                     headphones.CONFIG.OMGWTFNZBS)

    NZB_DOWNLOADERS = (headphones.CONFIG.SAB_HOST or
                       headphones.CONFIG.BLACKHOLE_DIR or
                       headphones.CONFIG.NZBGET_HOST)

    TORRENT_PROVIDERS = (headphones.CONFIG.TORZNAB or
                         headphones.CONFIG.PIRATEBAY or
                         headphones.CONFIG.RUTRACKER or
                         headphones.CONFIG.ORPHEUS or
                         headphones.CONFIG.REDACTED)

    BANDCAMP = 1 if (headphones.CONFIG.BANDCAMP and
                     headphones.CONFIG.BANDCAMP_DIR) else 0

    SOULSEEK = 1 if (headphones.CONFIG.SOULSEEK and
                     headphones.CONFIG.SOULSEEK_API_URL and
                     headphones.CONFIG.SOULSEEK_API_KEY and
                     headphones.CONFIG.SOULSEEK_DOWNLOAD_DIR and
                     headphones.CONFIG.SOULSEEK_INCOMPLETE_DOWNLOAD_DIR) else 0

    results = []
    myDB = db.DBConnection()
    albumlength = myDB.select('SELECT sum(TrackDuration) from tracks WHERE AlbumID=?',
                              [album['AlbumID']])[0][0]

    # NZBs
    if headphones.CONFIG.PREFER_TORRENTS == 0 and not choose_specific_download:
        if NZB_PROVIDERS and NZB_DOWNLOADERS:
            results = searchNZB(album, new, losslessOnly, albumlength)

        if not results and TORRENT_PROVIDERS:
            results = searchTorrent(album, new, losslessOnly, albumlength)

        if not results and BANDCAMP:
            results = searchBandcamp(album, new, albumlength)

        if not results and SOULSEEK:
            results = searchSoulseek(album, new, losslessOnly, albumlength)

    # Torrents
    elif headphones.CONFIG.PREFER_TORRENTS == 1 and not choose_specific_download:
        if TORRENT_PROVIDERS:
            results = searchTorrent(album, new, losslessOnly, albumlength)

        if not results and NZB_PROVIDERS and NZB_DOWNLOADERS:
            results = searchNZB(album, new, losslessOnly, albumlength)

        if not results and BANDCAMP:
            results = searchBandcamp(album, new, albumlength)

        if not results and SOULSEEK:
            results = searchSoulseek(album, new, losslessOnly, albumlength)

    # Soulseek
    elif headphones.CONFIG.PREFER_TORRENTS == 2 and not choose_specific_download:
        results = searchSoulseek(album, new, losslessOnly, albumlength)

        if not results and NZB_PROVIDERS and NZB_DOWNLOADERS:
            results = searchNZB(album, new, losslessOnly, albumlength)

        if not results and TORRENT_PROVIDERS:
            results = searchTorrent(album, new, losslessOnly, albumlength)

        if not results and BANDCAMP:
            results = searchBandcamp(album, new, albumlength)

    else:

        # No Preference
        nzb_results = []
        torrent_results = []
        bandcamp_results = []
        soulseek_results = []

        if NZB_PROVIDERS and NZB_DOWNLOADERS:
            nzb_results = searchNZB(album, new, losslessOnly,
                                    albumlength, choose_specific_download)

        if TORRENT_PROVIDERS:
            torrent_results = searchTorrent(album, new, losslessOnly,
                                            albumlength, choose_specific_download)

        if BANDCAMP:
            bandcamp_results = searchBandcamp(album, new, albumlength)

        # TODO: get this working
        # if SOULSEEK:
            # soulseek_results = searchSoulseek(album, new, losslessOnly,
            #                                  albumlength, choose_specific_download)

        results = nzb_results + torrent_results + bandcamp_results + soulseek_results

    if choose_specific_download:
        return results

    # Filter all results that do not comply
    results = [result for result in results if result.matches]

    # Sort the remaining results
    sorted_search_results = sort_search_results(results, album, new, albumlength)

    if not sorted_search_results:
        return

    logger.info(
        "Making sure we can download the best result: "
        f"{sorted_search_results[0].title} from {get_provider_name(sorted_search_results[0].provider)}"
    )
    (data, result) = preprocess(sorted_search_results)

    if data and result:
        send_to_downloader(data, result, album)


def more_filtering(results, album, albumlength, new):
    low_size_limit = None
    high_size_limit = None
    allow_lossless = False
    myDB = db.DBConnection()

    # Lossless - ignore results if target size outside bitrate range
    if headphones.CONFIG.PREFERRED_QUALITY == 3 and albumlength and (
            headphones.CONFIG.LOSSLESS_BITRATE_FROM or headphones.CONFIG.LOSSLESS_BITRATE_TO):
        if headphones.CONFIG.LOSSLESS_BITRATE_FROM:
            low_size_limit = albumlength / 1000 * int(headphones.CONFIG.LOSSLESS_BITRATE_FROM) * 128
        if headphones.CONFIG.LOSSLESS_BITRATE_TO:
            high_size_limit = albumlength / 1000 * int(headphones.CONFIG.LOSSLESS_BITRATE_TO) * 128

    # Preferred Bitrate - ignore results if target size outside % buffer
    elif headphones.CONFIG.PREFERRED_QUALITY == 2 and headphones.CONFIG.PREFERRED_BITRATE:
        logger.debug('Target bitrate: %s kbps' % headphones.CONFIG.PREFERRED_BITRATE)
        if albumlength:
            targetsize = albumlength / 1000 * int(headphones.CONFIG.PREFERRED_BITRATE) * 128
            logger.info('Target size: %s' % bytes_to_mb(targetsize))
            if headphones.CONFIG.PREFERRED_BITRATE_LOW_BUFFER:
                low_size_limit = targetsize - targetsize * int(
                    headphones.CONFIG.PREFERRED_BITRATE_LOW_BUFFER) / 100
            if headphones.CONFIG.PREFERRED_BITRATE_HIGH_BUFFER:
                high_size_limit = targetsize + targetsize * int(
                    headphones.CONFIG.PREFERRED_BITRATE_HIGH_BUFFER) / 100
                if headphones.CONFIG.PREFERRED_BITRATE_ALLOW_LOSSLESS:
                    allow_lossless = True

    newlist = []

    for result in results:

        if low_size_limit and result.size < low_size_limit:
            logger.info(
                f"{result.title} from {get_provider_name(result.provider)} is too small for this album. "
                f"(Size: {bytes_to_mb(result.size)}, MinSize: {bytes_to_mb(low_size_limit)})"
            )
            continue

        if high_size_limit and result.size > high_size_limit:
            logger.info(
                f"{result.title} from {get_provider_name(result.provider)} is too large for this album. "
                f"(Size: {bytes_to_mb(result.size)}, MaxSize: {bytes_to_mb(high_size_limit)})"
            )
            # Keep lossless results if there are no good lossy matches
            if not (allow_lossless and 'flac' in result.title.lower()):
                continue

        if new:
            alreadydownloaded = myDB.select(
                "SELECT * from snatched WHERE URL=?", [result.url]
            )
            if len(alreadydownloaded):
                logger.info(
                    f"{result.title} has already been downloaded from "
                    f"{get_provider_name(result.provider)}. Skipping."
                )
                continue

        newlist.append(result)

    return newlist


def sort_by_priority_then_size(rs):
    return list(map(lambda x: x[0],
        sorted(
            rs,
            key=lambda x: (x[0].matches, x[1], x[0].size),
            reverse=True
        )
    ))


def sort_search_results(resultlist, album, new, albumlength):
    if new and not len(resultlist):
        logger.info(
            'No more results found for:  %s - %s' % (album['ArtistName'], album['AlbumTitle']))
        return None

    # Add a priority if it has any of the preferred words
    results_with_priority = []
    preferred_words = split_string(headphones.CONFIG.PREFERRED_WORDS)
    for result in resultlist:
        priority = 0
        for word in preferred_words:
            if word.lower() in [result.title.lower(), result.provider.lower()]:
                priority += len(preferred_words) - preferred_words.index(word)
        results_with_priority.append((result, priority))

    if headphones.CONFIG.PREFERRED_QUALITY == 2 and headphones.CONFIG.PREFERRED_BITRATE:

        try:
            targetsize = albumlength / 1000 * int(headphones.CONFIG.PREFERRED_BITRATE) * 128
            if not targetsize:
                logger.info(
                    f"No track information for {album['ArtistName']} - "
                    f"{album['AlbumTitle']}. Defaulting to highest quality"
                )
                return sort_by_priority_then_size(results_with_priority)

            else:
                lossy_results_with_delta = []
                lossless_results = []

                for result, priority in results_with_priority:

                    # Add lossless results to the "flac list" which we can use if there are no good lossy matches
                    if 'flac' in result.title.lower():
                        lossless_results.append((result, priority))
                    else:
                        delta = abs(targetsize - result.size)
                        lossy_results_with_delta.append((result, priority, delta))

                if len(lossy_results_with_delta):
                    return list(map(lambda x: x[0],
                        sorted(
                            lossy_results_with_delta,
                            key=lambda x: (-x[0].matches, -x[1], x[2])
                        )
                    ))

                if (
                        not len(lossy_results_with_delta)
                        and len(lossless_results)
                        and headphones.CONFIG.PREFERRED_BITRATE_ALLOW_LOSSLESS
                    ):
                    logger.info(
                        "Since there were no appropriate lossy matches "
                        "(and at least one lossless match), going to use "
                        "lossless instead"
                    )
                    return sort_by_priority_then_size(lossless_results)

        except (KeyError, IndexError, AttributeError) as e:
            logger.warning('Missing album data or invalid format: %s', e)
            return sort_by_priority_then_size(results_with_priority)
        except Exception as e:
            logger.exception('Unhandled exception in search: %s', e)
            logger.info(
                f"No track information for {album['ArtistName']} - "
                f"{album['AlbumTitle']}. Defaulting to highest quality"
            )
            return sort_by_priority_then_size(results_with_priority)

    else:
        return sort_by_priority_then_size(results_with_priority)

    logger.info(
        f"No appropriate matches found for {album['ArtistName']} - "
        f"{album['AlbumTitle']}"
    )
    return None


def get_year_from_release_date(release_date):
    try:
        year = release_date[:4]
    except TypeError:
        year = ''

    return year


def searchBandcamp(album, new=False, albumlength=None):
    return bandcamp.search(album)


def searchNZB(album, new=False, losslessOnly=False, albumlength=None,
              choose_specific_download=False):
    reldate = album['ReleaseDate']
    year = get_year_from_release_date(reldate)

    replacements = {
        '...': '',
        ' & ': ' ',
        ' = ': ' ',
        '?': '',
        '$': 's',
        ' + ': ' ',
        '"': '',
        ',': '',
        '*': '',
        '.': '',
        ':': ''
    }

    cleanalbum = unidecode(replace_all(album['AlbumTitle'], replacements)).strip()
    cleanartist = unidecode(replace_all(album['ArtistName'], replacements)).strip()

    # Use the provided search term if available, otherwise build a search term
    if album['SearchTerm']:
        term = album['SearchTerm']
    elif album['Type'] == 'part of':
        term = cleanalbum + " " + year
    else:
        # FLAC usually doesn't have a year for some reason so leave it out.
        # Various Artist albums might be listed as VA, so I'll leave that out too
        # Only use the year if the term could return a bunch of different albums, i.e. self-titled albums
        if album['ArtistName'] in album['AlbumTitle'] or len(album['ArtistName']) < 4 or len(
                album['AlbumTitle']) < 4:
            term = cleanartist + ' ' + cleanalbum + ' ' + year
        elif album['ArtistName'] == 'Various Artists':
            term = cleanalbum + ' ' + year
        else:
            term = cleanartist + ' ' + cleanalbum

    # Replace bad characters in the term
    term = re.sub(r'[\.\-\/]', r' ', term)
    artistterm = re.sub(r'[\.\-\/]', r' ', cleanartist)

    # If Preferred Bitrate and High Limit and Allow Lossless then get both lossy and lossless
    if headphones.CONFIG.PREFERRED_QUALITY == 2 and headphones.CONFIG.PREFERRED_BITRATE and headphones.CONFIG.PREFERRED_BITRATE_HIGH_BUFFER and headphones.CONFIG.PREFERRED_BITRATE_ALLOW_LOSSLESS:
        allow_lossless = True
    else:
        allow_lossless = False

    logger.debug("Using search term: %s" % term)

    resultlist = []

    if headphones.CONFIG.HEADPHONES_INDEXER:
        provider = "headphones"

        if headphones.CONFIG.PREFERRED_QUALITY == 3 or losslessOnly:
            categories = "3040"
        elif headphones.CONFIG.PREFERRED_QUALITY == 1 or allow_lossless:
            categories = "3040,3010"
        else:
            categories = "3010"

        if album['Type'] == 'Other':
            logger.info("Album type is audiobook/spokenword. Using audiobook category")
            categories = "3030"

        # Request results
        logger.info('Searching Headphones Indexer with search term: %s' % term)

        headers = {'User-Agent': USER_AGENT}
        params = {
            "t": "search",
            "cat": categories,
            "apikey": '964d601959918a578a670984bdee9357',
            "maxage": headphones.CONFIG.USENET_RETENTION,
            "q": term
        }

        data = request.request_feed(
            url="https://indexer.codeshy.com/api",
            params=params, headers=headers,
            auth=(headphones.CONFIG.HPUSER, headphones.CONFIG.HPPASS)
        )

        # Process feed
        if data:
            if not len(data.entries):
                logger.info("No results found from %s for %s" % ('Headphones Index', term))
            else:
                for item in data.entries:
                    try:
                        url = item.link
                        title = item.title
                        size = int(item.links[1]['length'])

                        resultlist.append(Result(title, size, url, provider, 'nzb', True))
                        logger.info('Found %s. Size: %s' % (title, bytes_to_mb(size)))
                    except Exception as e:
                        logger.error("An unknown error occurred trying to parse the feed: %s" % e)

    if headphones.CONFIG.NEWZNAB:
        provider = "newznab"
        newznab_hosts = []

        if headphones.CONFIG.NEWZNAB_HOST and headphones.CONFIG.NEWZNAB_ENABLED:
            newznab_hosts.append((headphones.CONFIG.NEWZNAB_HOST, headphones.CONFIG.NEWZNAB_APIKEY,
                                  headphones.CONFIG.NEWZNAB_ENABLED))

        for newznab_host in headphones.CONFIG.get_extra_newznabs():
            if newznab_host[2] == '1' or newznab_host[2] == 1:
                newznab_hosts.append(newznab_host)

        if headphones.CONFIG.PREFERRED_QUALITY == 3 or losslessOnly:
            categories = "3040"
        elif headphones.CONFIG.PREFERRED_QUALITY == 1 or allow_lossless:
            categories = "3040,3010"
        else:
            categories = "3010"

        if album['Type'] == 'Other':
            categories = "3030"
            logger.info("Album type is audiobook/spokenword. Using audiobook category")

        for newznab_host in newznab_hosts:

            provider = newznab_host[0]

            # Add a little mod for kere.ws
            if newznab_host[0] == "https://kere.ws":
                if categories == "3040":
                    categories = categories + ",4070"
                elif categories == "3040,3010":
                    categories = categories + ",4070,4010"
                elif categories == "3010":
                    categories = categories + ",4010"
                else:
                    categories = categories + ",4050"

            # Request results
            logger.info('Parsing results from %s using search term: %s' % (newznab_host[0], term))

            headers = {'User-Agent': USER_AGENT}
            params = {
                "t": "search",
                "apikey": newznab_host[1],
                "cat": categories,
                "maxage": headphones.CONFIG.USENET_RETENTION,
                "q": term
            }

            data = request.request_feed(
                url=newznab_host[0] + '/api?',
                params=params, headers=headers
            )

            # Process feed
            if data:
                if not len(data.entries):
                    logger.info("No results found from %s for %s", newznab_host[0], term)
                else:
                    for item in data.entries:
                        try:
                            url = item.link
                            title = item.title

                            # Initialize size
                            size = 0

                            # Try to get the size attribute safely
                            if hasattr(item, 'links') and len(item.links) > 1:
                                if 'length' in item.links[1]:
                                    size = int(item.links[1]['length'])
                                elif hasattr(item, 'enclosures') and len(item.enclosures) > 0 and 'length' in item.enclosures[0]:
                                    size = int(item.enclosures[0]['length'])

                            # Fallback attempts
                            if size == 0:
                                if hasattr(item, 'size'):
                                    size = int(item.size)
                                elif hasattr(item, 'length'):
                                    size = int(item.length)
                                else:
                                    logger.debug('Could not determine size for %s', title)

                            if all(word.lower() in title.lower() for word in term.split()):
                                logger.info(
                                    'Found %s. Size: %s' % (title, bytes_to_mb(size)))
                                resultlist.append(Result(title, size, url, provider, 'nzb', True))
                            else:
                                logger.info('Skipping %s, not all search term words found' % title)

                        except Exception as e:
                            logger.exception(
                                "An unknown error occurred trying to parse the feed: %s" % e)

    if headphones.CONFIG.NZBSORG:
        provider = "nzbsorg"
        if headphones.CONFIG.PREFERRED_QUALITY == 3 or losslessOnly:
            categories = "3040"
        elif headphones.CONFIG.PREFERRED_QUALITY == 1 or allow_lossless:
            categories = "3040,3010"
        else:
            categories = "3010"

        if album['Type'] == 'Other':
            categories = "3030"
            logger.info("Album type is audiobook/spokenword. Using audiobook category")

        headers = {'User-Agent': USER_AGENT}
        params = {
            "t": "search",
            "apikey": headphones.CONFIG.NZBSORG_HASH,
            "cat": categories,
            "maxage": headphones.CONFIG.USENET_RETENTION,
            "q": term
        }

        data = request.request_feed(
            url='https://beta.nzbs.org/api',
            params=params, headers=headers,
            timeout=5
        )

        logger.info('Parsing results from nzbs.org using search term: %s' % term)
        # Process feed
        if data:
            if not len(data.entries):
                logger.info("No results found from nzbs.org for %s" % term)
            else:
                for item in data.entries:
                    try:
                        url = item.link
                        title = item.title
                        size = int(item.links[1]['length'])

                        resultlist.append(Result(title, size, url, provider, 'nzb', True))
                        logger.info('Found %s. Size: %s' % (title, bytes_to_mb(size)))
                    except Exception as e:
                        logger.exception("Unhandled exception while parsing feed")

    if headphones.CONFIG.OMGWTFNZBS:
        provider = "omgwtfnzbs"

        if headphones.CONFIG.PREFERRED_QUALITY == 3 or losslessOnly:
            categories = "22"
        elif headphones.CONFIG.PREFERRED_QUALITY == 1 or allow_lossless:
            categories = "22,7"
        else:
            categories = "7"

        if album['Type'] == 'Other':
            categories = "29"
            logger.info("Album type is audiobook/spokenword. Searching all music categories")

        # Request results
        logger.info('Parsing results from omgwtfnzbs using search term: %s' % term)

        headers = {'User-Agent': USER_AGENT}
        params = {
            "user": headphones.CONFIG.OMGWTFNZBS_UID,
            "api": headphones.CONFIG.OMGWTFNZBS_APIKEY,
            "catid": categories,
            "retention": headphones.CONFIG.USENET_RETENTION,
            "search": term
        }

        data = request.request_json(
            url='https://api.omgwtfnzbs.me/json/',
            params=params, headers=headers
        )

        # Parse response
        if data:
            if 'notice' in data:
                logger.info("No results returned from omgwtfnzbs: %s" % data['notice'])
            else:
                for item in data:
                    try:
                        url = item['getnzb']
                        title = item['release']
                        size = int(item['sizebytes'])

                        resultlist.append(Result(title, size, url, provider, 'nzb', True))
                        logger.info('Found %s. Size: %s', title, bytes_to_mb(size))
                    except Exception as e:
                        logger.exception("Unhandled exception")

    # attempt to verify that this isn't a substring result
    # when looking for "Foo - Foo" we don't want "Foobar"
    # this should be less of an issue when it isn't a self-titled album so we'll only check vs artist
    #
    # Also will filter flac & remix albums if not specifically looking for it
    # This code also checks the ignored words and required words
    results = [result for result in resultlist if
               verifyresult(result.title, artistterm, term, losslessOnly)]

    # Additional filtering for size etc
    if results and not choose_specific_download:
        results = more_filtering(results, album, albumlength, new)

    return results


def send_to_downloader(data, result, album):
    logger.info(
        f"Found best result from {get_provider_name(result.provider)}: <a href=\"{result.url}\">"
        f"{result.title}</a> - {bytes_to_mb(result.size)}"
    )
    # Get rid of any dodgy chars here so we can prevent sab from renaming our downloads
    kind = result.kind
    seed_ratio = None
    torrentid = None

    if kind == 'nzb':
        folder_name = sab_sanitize_foldername(result.title)

        if headphones.CONFIG.NZB_DOWNLOADER == 1:

            nzb = classes.NZBDataSearchResult()
            nzb.extraInfo.append(data)
            nzb.name = folder_name
            if not nzbget.sendNZB(nzb):
                return

        elif headphones.CONFIG.NZB_DOWNLOADER == 0:

            nzb = classes.NZBDataSearchResult()
            nzb.extraInfo.append(data)
            nzb.name = folder_name
            if not sab.sendNZB(nzb):
                return

            # If we sent the file to sab, we can check how it was renamed and insert that into the snatched table
            (replace_spaces, replace_dots) = sab.checkConfig()

            if replace_dots:
                folder_name = sab_replace_dots(folder_name)
            if replace_spaces:
                folder_name = sab_replace_spaces(folder_name)

        else:
            nzb_name = folder_name + '.nzb'
            download_path = os.path.join(headphones.CONFIG.BLACKHOLE_DIR, nzb_name)

            try:
                prev = os.umask(headphones.UMASK)

                with open(download_path, 'wb') as fp:
                    fp.write(data)

                os.umask(prev)
                logger.info('File saved to: %s', nzb_name)
            except Exception as e:
                logger.error('Couldn\'t write NZB file: %s', e)
                return

    elif kind == 'bandcamp':
        folder_name = bandcamp.download(album, result)
        logger.info("Setting folder_name to: {}".format(folder_name))

    elif kind == 'soulseek':
        try:
            soulseek.download(user=result.user, filelist=result.files)
            folder_name = '{' + result.user + '}' + result.folder
            logger.info(f"Soulseek folder name: {result.folder}")
        except Exception as e:
            logger.error(f"Soulseek error, check server logs: {e}")
            return


    else:
        folder_name = '%s - %s [%s]' % (
            unidecode(album['ArtistName']).replace('/', '_'),
            unidecode(album['AlbumTitle']).replace('/', '_'),
            get_year_from_release_date(album['ReleaseDate']))

        # Blackhole
        if headphones.CONFIG.TORRENT_DOWNLOADER == 0:

            # Get torrent name from .torrent, this is usually used by the torrent client as the folder name
            torrent_name = replace_illegal_chars(folder_name) + '.torrent'
            download_path = os.path.join(headphones.CONFIG.TORRENTBLACKHOLE_DIR, torrent_name)

            if result.url.lower().startswith("magnet:"):
                if headphones.CONFIG.MAGNET_LINKS == 1:
                    try:
                        if headphones.SYS_PLATFORM == 'win32':
                            os.startfile(result.url)
                        elif headphones.SYS_PLATFORM == 'darwin':
                            subprocess.Popen(["open", result.url], stdout=subprocess.PIPE,
                                             stderr=subprocess.PIPE)
                        else:
                            subprocess.Popen(["xdg-open", result.url], stdout=subprocess.PIPE,
                                             stderr=subprocess.PIPE)

                        # Gonna just take a guess at this..... Is there a better way to find this out?
                        folder_name = result.title
                    except Exception as e:
                        logger.error("Error opening magnet link: %s" % str(e))
                        return
                elif headphones.CONFIG.MAGNET_LINKS == 2:
                    # Procedure adapted from CouchPotato
                    torrent_hash = calculate_torrent_hash(result.url)

                    # Randomize list of services
                    services = TORRENT_TO_MAGNET_SERVICES[:]
                    random.shuffle(services)
                    headers = {'User-Agent': USER_AGENT}

                    for service in services:

                        data = request.request_content(service % torrent_hash, headers=headers)
                        if data:
                            if not torrent_to_file(download_path, data):
                                return
                            # Extract folder name from torrent
                            folder_name = read_torrent_name(
                                    download_path,
                                    result.title)

                            # Break for loop
                            break
                    else:
                        # No service succeeded
                        logger.warning("Unable to convert magnet with hash "
                                       "'%s' into a torrent file.", torrent_hash)
                        return
                elif headphones.CONFIG.MAGNET_LINKS == 3:
                    torrent_to_file(download_path, data)
                    return
                else:
                    logger.error("Cannot save magnet link in blackhole. "
                                 "Please switch your torrent downloader to "
                                 "Transmission, uTorrent or Deluge, or allow Headphones "
                                 "to open or convert magnet links")
                    return
            else:

                if not torrent_to_file(download_path, data):
                    return

                # Extract folder name from torrent
                folder_name = read_torrent_name(download_path, result.title)
                if folder_name:
                    logger.info('Torrent folder name: %s' % folder_name)

        elif headphones.CONFIG.TORRENT_DOWNLOADER == 1:
            logger.info("Sending torrent to Transmission")

            # Add torrent
            if result.provider == 'rutracker.org':
                torrentid = transmission.addTorrent('', data)
            else:
                torrentid = transmission.addTorrent(result.url)

            if not torrentid:
                logger.error("Error sending torrent to Transmission. Are you sure it's running?")
                return

            folder_name = transmission.getName(torrentid)
            if folder_name:
                logger.info('Torrent name: %s' % folder_name)
            else:
                logger.error('Torrent name could not be determined')
                return

            # Set Seed Ratio
            seed_ratio = get_seed_ratio(result.provider)
            if seed_ratio is not None:
                transmission.setSeedRatio(torrentid, seed_ratio)

        elif headphones.CONFIG.TORRENT_DOWNLOADER == 3:  # Deluge
            logger.info("Sending torrent to Deluge")

            try:
                # Add torrent
                if result.provider == 'rutracker.org':
                    torrentid = deluge.addTorrent('', data)
                else:
                    torrentid = deluge.addTorrent(result.url)

                if not torrentid:
                    logger.error("Error sending torrent to Deluge. Are you sure it's running? Maybe the torrent already exists?")
                    return

                # Set Label
                if headphones.CONFIG.DELUGE_LABEL:
                    deluge.setTorrentLabel({'hash': torrentid})

                # Set Seed Ratio
                seed_ratio = get_seed_ratio(result.provider)
                if seed_ratio is not None:
                    deluge.setSeedRatio({'hash': torrentid, 'ratio': seed_ratio})

                # Get folder name from Deluge, it's usually the torrent name
                folder_name = deluge.getTorrentFolder({'hash': torrentid})
                if folder_name:
                    logger.info('Torrent folder name: %s' % folder_name)
                else:
                    logger.error('Torrent folder name could not be determined')
                    return

            except Exception as e:
                logger.error('Error sending torrent to Deluge: %s' % str(e))

        elif headphones.CONFIG.TORRENT_DOWNLOADER == 2:
            logger.info("Sending torrent to uTorrent")

            # Add torrent
            if result.provider == 'rutracker.org':
                ruobj.utorrent_add_file(data)
            else:
                utorrent.addTorrent(result.url)

            # Get hash
            torrentid = calculate_torrent_hash(result.url, data)
            if not torrentid:
                logger.error('Torrent id could not be determined')
                return

            # Get folder
            folder_name = utorrent.getFolder(torrentid)
            if folder_name:
                logger.info('Torrent folder name: %s' % folder_name)
            else:
                logger.error('Torrent folder name could not be determined')
                return

            # Set Label
            if headphones.CONFIG.UTORRENT_LABEL:
                utorrent.labelTorrent(torrentid)

            # Set Seed Ratio
            seed_ratio = get_seed_ratio(result.provider)
            if seed_ratio is not None:
                utorrent.setSeedRatio(torrentid, seed_ratio)
        else:  # if headphones.CONFIG.TORRENT_DOWNLOADER == 4:
            logger.info("Sending torrent to QBiTorrent")

            # Add torrent
            if result.provider == 'rutracker.org':
                if qbittorrent.apiVersion2:
                    qbittorrent.addFile(data)
                else:
                    ruobj.qbittorrent_add_file(data)
            else:
                qbittorrent.addTorrent(result.url)

            # Get hash
            torrentid = calculate_torrent_hash(result.url, data)
            torrentid = torrentid.lower()
            if not torrentid:
                logger.error('Torrent id could not be determined')
                return

            # Get name
            folder_name = qbittorrent.getName(torrentid)
            if folder_name:
                logger.info('Torrent name: %s' % folder_name)
            else:
                logger.error('Torrent name could not be determined')
                return

            # Set Seed Ratio
            # Oh my god why is this repeated again for the 100th time
            seed_ratio = get_seed_ratio(result.provider)
            if seed_ratio is not None:
                qbittorrent.setSeedRatio(torrentid, seed_ratio)

    myDB = db.DBConnection()
    myDB.action('UPDATE albums SET status = "Snatched" WHERE AlbumID=?', [album['AlbumID']])
    myDB.action(
        "INSERT INTO snatched VALUES (?, ?, ?, ?, DATETIME('NOW', 'localtime'), "
        "?, ?, ?, ?)", [
            album['AlbumID'],
            result.title,
            result.size,
            result.url,
            "Snatched",
            folder_name,
            kind,
            torrentid
        ]
    )

    # Additional record for post processing or scheduled job to remove the torrent when finished seeding
    if seed_ratio is not None and seed_ratio != 0 and torrentid:
        myDB.action(
            "INSERT INTO snatched VALUES (?, ?, ?, ?, DATETIME('NOW', 'localtime'), "
            "?, ?, ?, ?)", [
                album['AlbumID'],
                result.title,
                result.size,
                result.url,
                "Seed_Snatched",
                folder_name,
                kind,
                torrentid
            ]
        )

    # notify
    artist = album[1]
    albumname = album[2]
    rgid = album[6]
    title = artist + ' - ' + albumname
    provider = get_provider_name(result.provider)
    name = folder_name if folder_name else None

    if headphones.CONFIG.GROWL_ENABLED and headphones.CONFIG.GROWL_ONSNATCH:
        logger.info("Sending Growl notification")
        growl = notifiers.GROWL()
        growl.notify(name, "Download started")
    if headphones.CONFIG.PROWL_ENABLED and headphones.CONFIG.PROWL_ONSNATCH:
        logger.info("Sending Prowl notification")
        prowl = notifiers.PROWL()
        prowl.notify(name, "Download started")
    if headphones.CONFIG.PUSHOVER_ENABLED and headphones.CONFIG.PUSHOVER_ONSNATCH:
        logger.info("Sending Pushover notification")
        prowl = notifiers.PUSHOVER()
        prowl.notify(name, "Download started")
    if headphones.CONFIG.PUSHBULLET_ENABLED and headphones.CONFIG.PUSHBULLET_ONSNATCH:
        logger.info("Sending PushBullet notification")
        pushbullet = notifiers.PUSHBULLET()
        pushbullet.notify(name, "Download started")
    if headphones.CONFIG.JOIN_ENABLED and headphones.CONFIG.JOIN_ONSNATCH:
        logger.info("Sending Join notification")
        join = notifiers.JOIN()
        join.notify(name, "Download started")
    if headphones.CONFIG.SLACK_ENABLED and headphones.CONFIG.SLACK_ONSNATCH:
        logger.info("Sending Slack notification")
        slack = notifiers.SLACK()
        slack.notify(name, "Download started")
    if headphones.CONFIG.TELEGRAM_ENABLED and headphones.CONFIG.TELEGRAM_ONSNATCH:
        logger.info("Sending Telegram notification")
        from headphones import cache
        c = cache.Cache()
        album_art = c.get_artwork_from_cache(None, rgid)
        telegram = notifiers.TELEGRAM()
        message = 'Snatched from ' + provider + '. ' + name
        telegram.notify(message, "Snatched: " + title, rgid, image=album_art)
    if headphones.CONFIG.TWITTER_ENABLED and headphones.CONFIG.TWITTER_ONSNATCH:
        logger.info("Twitter notifications temporarily disabled")
        #logger.info("Sending Twitter notification")
        #twitter = notifiers.TwitterNotifier()
        #twitter.notify_snatch(name)
    if headphones.CONFIG.NMA_ENABLED and headphones.CONFIG.NMA_ONSNATCH:
        logger.info("Sending NMA notification")
        nma = notifiers.NMA()
        nma.notify(snatched=name)
    if headphones.CONFIG.PUSHALOT_ENABLED and headphones.CONFIG.PUSHALOT_ONSNATCH:
        logger.info("Sending Pushalot notification")
        pushalot = notifiers.PUSHALOT()
        pushalot.notify(name, "Download started")
    if headphones.CONFIG.OSX_NOTIFY_ENABLED and headphones.CONFIG.OSX_NOTIFY_ONSNATCH:
        from headphones import cache
        c = cache.Cache()
        album_art = c.get_artwork_from_cache(None, rgid)
        logger.info("Sending OS X notification")
        osx_notify = notifiers.OSX_NOTIFY()
        osx_notify.notify(artist,
                          albumname,
                          'Snatched: ' + provider + '. ' + name,
                          image=album_art)
    if headphones.CONFIG.BOXCAR_ENABLED and headphones.CONFIG.BOXCAR_ONSNATCH:
        logger.info("Sending Boxcar2 notification")
        b2msg = 'From ' + provider + '<br></br>' + name
        boxcar = notifiers.BOXCAR()
        boxcar.notify('Headphones snatched: ' + title, b2msg, rgid)
    if headphones.CONFIG.EMAIL_ENABLED and headphones.CONFIG.EMAIL_ONSNATCH:
        logger.info("Sending Email notification")
        email = notifiers.Email()
        message = 'Snatched from ' + provider + '. ' + name
        email.notify("Snatched: " + title, message)


def verifyresult(title, artistterm, term, lossless):
    title = re.sub(r'[\.\-\/\_]', r' ', title)

    # if artistterm != 'Various Artists':
    #
    #    if not re.search('^' + re.escape(artistterm), title, re.IGNORECASE):
    #        #logger.info("Removed from results: " + title + " (artist not at string start).")
    #        #return False
    #    elif re.search(re.escape(artistterm) + '\w', title, re.IGNORECASE | re.UNICODE):
    #        logger.info("Removed from results: " + title + " (post substring result).")
    #        return False
    #    elif re.search('\w' + re.escape(artistterm), title, re.IGNORECASE | re.UNICODE):
    #        logger.info("Removed from results: " + title + " (pre substring result).")
    #        return False

    # another attempt to weed out substrings. We don't want "Vol III" when we were looking for "Vol II"

    # Filter out remix search results (if we're not looking for it)
    if 'remix' not in term.lower() and 'remix' in title.lower():
        logger.info(
            "Removed %s from results because it's a remix album and we're not looking for a remix album right now.",
            title)
        return False

    # Filter out FLAC if we're not specifically looking for it
    if (headphones.CONFIG.PREFERRED_QUALITY == 0 or headphones.CONFIG.PREFERRED_QUALITY == '0') \
            and 'flac' in title.lower() and not lossless:
        logger.info(
            "Removed %s from results because it's a lossless album and we're not looking for a lossless album right now.",
            title)
        return False

    if headphones.CONFIG.IGNORED_WORDS:
        for each_word in split_string(headphones.CONFIG.IGNORED_WORDS):
            if each_word.lower() in title.lower():
                logger.info("Removed '%s' from results because it contains ignored word: '%s'",
                            title, each_word)
                return False

    if headphones.CONFIG.REQUIRED_WORDS:
        for each_word in split_string(headphones.CONFIG.REQUIRED_WORDS):
            if ' OR ' in each_word:
                or_words = split_string(each_word, 'OR')
                if any(word.lower() in title.lower() for word in or_words):
                    continue
                else:
                    logger.info(
                        "Removed '%s' from results because it doesn't contain any of the required words in: '%s'",
                        title, str(or_words))
                    return False
            if each_word.lower() not in title.lower():
                logger.info(
                    "Removed '%s' from results because it doesn't contain required word: '%s'",
                    title, each_word)
                return False

    if headphones.CONFIG.IGNORE_CLEAN_RELEASES:
        for each_word in ['clean', 'edited', 'censored']:
            # logger.debug("Checking if '%s' is in search result: '%s'", each_word, title)
            if each_word.lower() in title.lower() and each_word.lower() not in term.lower():
                logger.info("Removed '%s' from results because it contains clean album word: '%s'",
                            title, each_word)
                return False

    tokens = re.split(r'\W', term, re.IGNORECASE | re.UNICODE)

    for token in tokens:

        if not token:
            continue
        if token == 'Various' or token == 'Artists' or token == 'VA':
            continue
        if not has_token(title, token):
            cleantoken = ''.join(c for c in token if c not in string.punctuation)
            if not has_token(title, cleantoken):
                dic = {'!': 'i', '$': 's'}
                dumbtoken = replace_all(token, dic)
                if not has_token(title, dumbtoken):
                    logger.info(
                        "Removed from results: %s (missing tokens: [%s, %s, %s])",
                        title, token, cleantoken, dumbtoken)
                    return False

    return True


def searchTorrent(album, new=False, losslessOnly=False, albumlength=None,
                  choose_specific_download=False):
    global orpheusobj  # persistent orpheus.network api object to reduce number of login attempts
    global redobj  # persistent redacted api object to reduce number of login attempts
    global ruobj  # and rutracker

    reldate = album['ReleaseDate']

    year = get_year_from_release_date(reldate)

    # MERGE THIS WITH THE TERM CLEANUP FROM searchNZB
    replacements = {
        '...': '',
        ' & ': ' ',
        ' = ': ' ',
        '?': '',
        '$': 's',
        ' + ': ' ',
        '"': '',
        ',': ' ',
        '*': ''
    }

    semi_cleanalbum = replace_all(album['AlbumTitle'], replacements)
    cleanalbum = unidecode(semi_cleanalbum)
    semi_cleanartist = replace_all(album['ArtistName'], replacements)
    cleanartist = unidecode(semi_cleanartist)

    # Use provided term if available, otherwise build our own (this code needs to be cleaned up since a lot
    # of these torrent providers are just using cleanartist/cleanalbum terms
    if album['SearchTerm']:
        term = album['SearchTerm']
    elif album['Type'] == 'part of':
        term = cleanalbum + " " + year
    else:
        # FLAC usually doesn't have a year for some reason so I'll leave it out
        # Various Artist albums might be listed as VA, so I'll leave that out too
        # Only use the year if the term could return a bunch of different albums, i.e. self-titled albums
        if album['ArtistName'] in album['AlbumTitle'] or len(album['ArtistName']) < 4 or len(
                album['AlbumTitle']) < 4:
            term = cleanartist + ' ' + cleanalbum + ' ' + year
        elif album['ArtistName'] == 'Various Artists':
            term = cleanalbum + ' ' + year
        else:
            term = cleanartist + ' ' + cleanalbum

    # Save user search term
    if album['SearchTerm']:
        usersearchterm = term
    else:
        usersearchterm = ''

    semi_clean_artist_term = re.sub(r'[\.\-\/]', r' ', semi_cleanartist)
    semi_clean_album_term = re.sub(r'[\.\-\/]', r' ', semi_cleanalbum)
    # Replace bad characters in the term
    term = re.sub(r'[\.\-\/]', r' ', term)
    artistterm = re.sub(r'[\.\-\/]', r' ', cleanartist)
    albumterm = re.sub(r'[\.\-\/]', r' ', cleanalbum)

    # If Preferred Bitrate and High Limit and Allow Lossless then get both lossy and lossless
    if headphones.CONFIG.PREFERRED_QUALITY == 2 and headphones.CONFIG.PREFERRED_BITRATE and headphones.CONFIG.PREFERRED_BITRATE_HIGH_BUFFER and headphones.CONFIG.PREFERRED_BITRATE_ALLOW_LOSSLESS:
        allow_lossless = True
    else:
        allow_lossless = False

    logger.debug("Using search term: %s" % term)

    resultlist = []
    minimumseeders = int(headphones.CONFIG.NUMBEROFSEEDERS) - 1

    def set_proxy(proxy_url):
        if not proxy_url.startswith('http'):
            proxy_url = 'https://' + proxy_url
        if proxy_url.endswith('/'):
            proxy_url = proxy_url[:-1]

        return proxy_url

    if headphones.CONFIG.TORZNAB:
        provider = "torznab"
        torznab_hosts = []

        if headphones.CONFIG.TORZNAB_HOST and headphones.CONFIG.TORZNAB_ENABLED:
            torznab_hosts.append((headphones.CONFIG.TORZNAB_HOST, headphones.CONFIG.TORZNAB_APIKEY,
                                  headphones.CONFIG.TORZNAB_RATIO, headphones.CONFIG.TORZNAB_ENABLED))

        for torznab_host in headphones.CONFIG.get_extra_torznabs():
            if torznab_host[3] == '1' or torznab_host[3] == 1:
                torznab_hosts.append(torznab_host)

        parent_category = "3000"

        if headphones.CONFIG.PREFERRED_QUALITY == 3 or losslessOnly:
            categories = "3040"
            maxsize = 10000000000
        elif headphones.CONFIG.PREFERRED_QUALITY == 1 or allow_lossless:
            categories = "3040,3010,3050"
            maxsize = 10000000000
        else:
            categories = "3010,3050"
            maxsize = 300000000

        if album['Type'] == 'Other':
            categories = "3030"
            logger.info("Album type is audiobook/spokenword. Using audiobook category")

        categories = categories + "," + parent_category

        for torznab_host in torznab_hosts:

            provider = torznab_host[0]
            provider_name = torznab_host[0]

            # Format Jackett provider
            if "api/v2.0/indexers" in torznab_host[0]:
                provider_name = provider.split("/indexers/", 1)[1].split('/', 1)[0]
                provider = "Torznab" + '|' +  provider_name + '|' + torznab_host[0]

            # Request results
            logger.info('Parsing results from Torznab %s using search term: %s' % (provider_name, term))

            headers = {'User-Agent': USER_AGENT}
            params = {
                "t": "search",
                "apikey": torznab_host[1],
                #"cat": categories,
                "cat": parent_category, # search using '3000' and filter below
                #"maxage": headphones.CONFIG.USENET_RETENTION,
                "q": term
            }

            data = request.request_soup(
                url=torznab_host[0],
                params=params, headers=headers
            )

            # Process feed
            if data:
                items = data.find_all('item')
                if not items:
                    logger.info("No results found from %s for %s", provider_name, term)
                else:
                    for item in items:
                        try:
                            title = item.title.get_text()
                            if item.find("link"):
                                url = item.find("link").next_sibling.strip()
                            else:
                                url = item.find('enclosure').get('url')
                            seeders = int(item.find("torznab:attr", attrs={"name": "seeders"}).get('value'))
                            if item.size:
                                size = int(item.size.string)
                            else:
                                size = int(item.find("torznab:attr", attrs={"name": "size"}).get('value'))

                            category = item.find("torznab:attr", attrs={"name": "category"}).get('value')
                            if category not in categories:
                                logger.info(f"Skipping {title}, size {bytes_to_mb(size)}, incorrect category {category}")
                                continue

                            if all(word.lower() in title.lower() for word in term.split()):
                                if size < maxsize and minimumseeders < seeders:
                                    logger.info('Found %s. Size: %s' % (title, bytes_to_mb(size)))
                                    if item.prowlarrindexer:
                                        provider = "Torznab" + '|' + item.prowlarrindexer.get_text() + '|' + \
                                                   torznab_host[0]
                                    elif item.jackettindexer:
                                            provider = "Torznab" + '|' + item.jackettindexer.get_text() + '|' + \
                                                   torznab_host[0]
                                    resultlist.append(Result(title, size, url, provider, 'torrent', True))
                                else:
                                    logger.info(
                                        '%s is larger than the maxsize or has too little seeders for this category, '
                                        'skipping. (Size: %s, Seeders: %d)',
                                        title, bytes_to_mb(size), seeders)
                            else:
                                logger.info('Skipping %s, not all search term words found' % title)

                        except Exception as e:
                            logger.exception(
                                "An unknown error occurred trying to parse the feed: %s" % e)

    # rutracker.org
    if headphones.CONFIG.RUTRACKER:
        provider = "rutracker.org"

        # Ignore if release date not specified, results too unpredictable
        if not year and not usersearchterm:
            logger.info("Release date not specified, ignoring for rutracker.org")
        else:
            if headphones.CONFIG.PREFERRED_QUALITY == 3 or losslessOnly:
                format = 'lossless'
            elif headphones.CONFIG.PREFERRED_QUALITY == 1 or allow_lossless:
                format = 'lossless+mp3'
            else:
                format = 'mp3'

            # Login
            if not ruobj or not ruobj.logged_in():
                ruobj = rutracker.Rutracker()
                if not ruobj.login():
                    ruobj = None

            if ruobj and ruobj.logged_in():

                # build search url
                if not usersearchterm:
                    searchURL = ruobj.searchurl(artistterm, albumterm, year, format)
                else:
                    searchURL = ruobj.searchurl(usersearchterm, ' ', ' ', format)

                # parse results
                rulist = ruobj.search(searchURL)
                if rulist:
                    resultlist.extend(rulist)

    # RED, Orpheus.network and potentially other Gazelle API based trackers.

    def _search_torrent_gazelle(provider, providerurl, username=None, password=None, apikey=None, try_use_fltoken=False):
        global gazelleobjs
        bitrate = None
        bitrate_string = bitrate

        if headphones.CONFIG.PREFERRED_QUALITY == 3 or losslessOnly:  # Lossless Only mode
            search_formats = [gazelleformat.FLAC]
            maxsize = 10000000000
        elif headphones.CONFIG.PREFERRED_QUALITY == 2:  # Preferred quality mode
            search_formats = [None]  # should return all
            bitrate = headphones.CONFIG.PREFERRED_BITRATE
            if bitrate:
                if 225 <= int(bitrate) < 256:
                    bitrate = 'V0'
                elif 200 <= int(bitrate) < 225:
                    bitrate = 'V1'
                elif 175 <= int(bitrate) < 200:
                    bitrate = 'V2'
                for encoding_string in gazelleencoding.ALL_ENCODINGS:
                    if re.search(bitrate, encoding_string, flags=re.I):
                        bitrate_string = encoding_string
                if bitrate_string not in gazelleencoding.ALL_ENCODINGS:
                    logger.info(
                        f"Your preferred bitrate is not one of the available { provider } filters, so not using it as a search parameter.")
            maxsize = 10000000000
        elif headphones.CONFIG.PREFERRED_QUALITY == 1 or allow_lossless:  # Highest quality including lossless
            search_formats = [gazelleformat.FLAC, gazelleformat.MP3]
            maxsize = 10000000000
        else:  # Highest quality excluding lossless
            search_formats = [gazelleformat.MP3]
            maxsize = 300000000

        gazelleobj = gazelleobjs.get(provider, None)
        if not gazelleobj or not gazelleobj.logged_in():
            try:
                logger.info(f"Attempting to log in to {provider}...")
                if apikey:
                    gazelleobj = gazelleapi.GazelleAPI(apikey=apikey,
                                                        url=providerurl)
                elif username and password:
                    gazelleobj = gazelleapi.GazelleAPI(username=username,
                                                        password=password,
                                                        url=providerurl)
                else:
                    raise ValueError(f"Neither apikey nor username/password provided for provider {provider}.")
                gazelleobj._login()
            except Exception as e:
                gazelleobj = None
                logger.error("%s credentials incorrect or site is down. Error: %s %s" % (
                    provider, e.__class__.__name__, str(e)))
            gazelleobjs[provider] = gazelleobj

        if gazelleobj and gazelleobj.logged_in():
            logger.info("Searching %s..." % provider)
            all_torrents = []

            album_type = ""

            # Specify release types to filter by
            gazelle_release_type_mapping = {
                'Album': [gazellerelease_type.ALBUM],
                'Soundtrack': [gazellerelease_type.SOUNDTRACK],
                'EP': [gazellerelease_type.EP],
                # No musicbrainz match for this type
                # 'Anthology': [gazellerelease_type.ANTHOLOGY],
                'Compilation': [gazellerelease_type.COMPILATION],
                'DJ-mix': [gazellerelease_type.DJ_MIX],
                'Single': [gazellerelease_type.SINGLE],
                'Live': [gazellerelease_type.LIVE_ALBUM],
                'Remix': [gazellerelease_type.REMIX],
                'Bootleg': [gazellerelease_type.BOOTLEG],
                'Interview': [gazellerelease_type.INTERVIEW],
                'Mixtape/Street': [gazellerelease_type.MIXTAPE],
                'Other': [gazellerelease_type.UNKNOWN],
            }

            album_type = gazelle_release_type_mapping.get(
                album['Type'],
                gazellerelease_type.UNKNOWN
            )

            for search_format in search_formats:
                if usersearchterm:
                    all_torrents.extend(
                        gazelleobj.search_torrents(
                            searchstr=usersearchterm,
                            format=search_format,
                            encoding=bitrate_string,
                            releasetype=album_type
                        )['results']
                    )
                else:
                    all_torrents.extend(
                        gazelleobj.search_torrents(
                            artistname=semi_clean_artist_term,
                            groupname=semi_clean_album_term,
                            format=search_format,
                            encoding=bitrate_string,
                            releasetype=album_type)
                        ['results']
                    )

            # filter on format, size, and num seeders
            logger.info("Filtering torrents by format, maximum size, and minimum seeders...")
            match_torrents = [t for t in all_torrents if
                              t.size <= maxsize and t.seeders >= minimumseeders]

            logger.info(
                "Remaining torrents: %s" % ", ".join(repr(torrent) for torrent in match_torrents))

            # Sort by quality and seeders
            if not len(match_torrents):
                logger.info("No results found from %s for %s after filtering" % (provider, term))
            elif len(match_torrents) > 1:
                logger.info("Found %d matching releases from %s for %s - %s after filtering" %
                            (len(match_torrents), provider, artistterm, albumterm))
                if search_formats and None not in search_formats:
                    logger.info('Sorting torrents by format and number of seeders...')
                    match_torrents.sort(key=lambda x: (search_formats.index(x.format), -int(x.seeders)))
                else:
                    logger.info('Sorting torrents by number of seeders...')
                    match_torrents.sort(key=lambda x: int(x.seeders), reverse=True)
                logger.info(
                    "New order: %s" %
                    ", ".join(repr(torrent) for torrent in match_torrents)
                )

            results = []
            for torrent in match_torrents:
                if not torrent.file_path:
                    torrent.group.update_group_data()  # will load the file_path for the individual torrents

                use_fltoken = try_use_fltoken and torrent.can_use_token

                results.append(
                    Result(
                        torrent.file_path,
                        torrent.size,
                        gazelleobj.generate_torrent_link(torrent.id, use_fltoken),
                        provider,
                        'torrent',
                        True
                    )
                )
            return results

    if headphones.CONFIG.ORPHEUS:
        provider = "Orpheus.network"
        providerurl = "https://orpheus.network/"

        resultlist.extend(
            _search_torrent_gazelle(
                provider,
                providerurl,
                username=headphones.CONFIG.ORPHEUS_USERNAME,
                password=headphones.CONFIG.ORPHEUS_PASSWORD,
                try_use_fltoken=False,
            )
        )


    if headphones.CONFIG.REDACTED:
        provider = "Redacted"
        providerurl = "https://redacted.sh"

        resultlist.extend(
            _search_torrent_gazelle(
                provider,
                providerurl,
                username=headphones.CONFIG.REDACTED_USERNAME,
                password=headphones.CONFIG.REDACTED_PASSWORD,
                apikey=headphones.CONFIG.REDACTED_APIKEY,
                try_use_fltoken=headphones.CONFIG.REDACTED_USE_FLTOKEN,
            )
        )

    # PIRATE BAY

    # 09/08/2024 - thepiratebay.org no longer working, switch to apibay.org as default

    # Pirate Bay
    if (headphones.CONFIG.PIRATEBAY):
        logger.info(f"Searching The Pirate Bay using term: {term}")
        provider = "The Pirate Bay"
        tpb_term = term.replace("!", "").replace("'", " ").replace(" ", "%20")

        # Pick category for torrents
        if headphones.CONFIG.PREFERRED_QUALITY == 3 or losslessOnly:
            category = '104'  # FLAC
            maxsize = 10000000000
        elif headphones.CONFIG.PREFERRED_QUALITY == 1 or allow_lossless:
            category = '100'  # General audio category
            maxsize = 10000000000
        else:
            category = '101'  # MP3 only
            maxsize = 300000000

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 6.3; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/41.0.2243.2 Safari/537.36'}

        # Use proxy if specified
        if headphones.CONFIG.PIRATEBAY_PROXY_URL and "apibay.org" not in headphones.CONFIG.PIRATEBAY_PROXY_URL:
            apibay = False
            providerurl = fix_url(set_proxy(headphones.CONFIG.PIRATEBAY_PROXY_URL))
            providerurl = providerurl + "/search/" + tpb_term + "/0/7/"  # 7 is sort by seeders
            data = request.request_soup(url=providerurl + category, headers=headers)
            rows = []
            if data:
                rows = data.select('table tbody tr')[1:]
                if not rows:
                    rows = data.select('table tr')[1:]
        else:
            # Use apibay
            apibay = True
            rows = request.request_json(f"http://apibay.org/q.php?q={term}&cat={category}", headers=headers)

        for item in rows:
            # apibay
            if apibay:
                title = item["name"]
                if title == "No results returned":
                    rows = None
                    break
                size = int(item["size"])
                seeders = int(item["seeders"])
                url = pirate_bay_get_magnet(item["info_hash"], item["name"])
            else:
                # proxy
                try:
                    # proxy format 1
                    columns = item.find_all('td')
                    description = columns[1].text.strip().split('\n\n')
                    title = description[0]
                    url = columns[3].select('a[href^="magnet"]')[0]['href']
                    formatted_size = columns[4].text.replace('\xa0', ' ')
                    size = piratesize(formatted_size)
                    seeders = int(columns[5].text)
                except:
                    # proxy format 2
                    try:
                        title = ''.join(item.find("a", {"class": "detLink"}))
                        seeders = int(''.join(item.find("td", {"align": "right"})))
                        url = item.findAll("a")[3]["href"]
                        formatted_size = re.search('Size (.*),', str(item)).group(1).replace('\xa0', ' ')
                        size = piratesize(formatted_size)
                    except Exception as e:
                        logger.error("Cannot parse results with this proxy, leave setting blank for default apibay.org "
                                    f"or try a different proxy. Error: {e}")
                        break

            if size < maxsize and minimumseeders < seeders and url is not None:
                match = True
                logger.info(f"Found {title}. Size: {bytes_to_mb(size)}")
            else:
                match = False
                logger.info(f"{title} is larger than the maxsize or has too little seeders for this category, skipping."
                            f" (Size: {bytes_to_mb(size)}, Seeders: {seeders})")

            resultlist.append(Result(title, size, url, provider, "torrent", match))

        if not rows:
            logger.info(f"No valid results found from The Pirate Bay using term: {term}")

    # attempt to verify that this isn't a substring result
    # when looking for "Foo - Foo" we don't want "Foobar"
    # this should be less of an issue when it isn't a self-titled album so we'll only check vs artist
    results = [result for result in resultlist if verifyresult(result.title, artistterm, term, losslessOnly)]

    # Additional filtering for size etc
    if results and not choose_specific_download:
        results = more_filtering(results, album, albumlength, new)

    return results


def searchSoulseek(album, new=False, losslessOnly=False, albumlength=None,
                   choose_specific_download=False):
    # Not using some of the input stuff for now or ever
    replacements = {
        '...': '',
        ' & ': ' ',
        ' = ': ' ',
        '?': '',
        '$': '',
        ' + ': ' ',
        '"': '',
        ',': '',
        '*': '',
        '.': '',
        ':': ''
    }

    num_tracks = get_album_track_count(album['AlbumID'])
    year = get_year_from_release_date(album['ReleaseDate'])
    cleanalbum = unidecode(replace_all(album['AlbumTitle'], replacements)).strip()
    cleanartist = unidecode(replace_all(album['ArtistName'], replacements)).strip()

    # If Preferred Bitrate and High Limit and Allow Lossless then get both lossy and lossless
    if headphones.CONFIG.PREFERRED_QUALITY == 2 and headphones.CONFIG.PREFERRED_BITRATE and headphones.CONFIG.PREFERRED_BITRATE_HIGH_BUFFER and headphones.CONFIG.PREFERRED_BITRATE_ALLOW_LOSSLESS:
        allow_lossless = True
    else:
        allow_lossless = False

    if headphones.CONFIG.PREFERRED_QUALITY == 3 :
        losslessOnly = True
    elif headphones.CONFIG.PREFERRED_QUALITY == 1:
        allow_lossless = True

    if album['SearchTerm']:
        term = album['SearchTerm']
    else:
        term = ''

    try:
        resultlist = soulseek.search(artist=cleanartist, album=cleanalbum, year=year, losslessOnly=losslessOnly,
                                  allow_lossless=allow_lossless, num_tracks=num_tracks, user_search_term=term)

        if not resultlist:
            logger.info("No valid results found from Soulseek")

        # filter results
        results = [result for result in resultlist if verifyresult(result.title, cleanartist, term, losslessOnly)]

        # Additional filtering for size etc
        if results and not choose_specific_download:
            results = more_filtering(results, album, albumlength, new)

        return results

    except Exception as e:
        logger.error(f"Soulseek error, check server logs: {e}")
        return None


def get_album_track_count(album_id):
    # Not sure if this should be considered a helper function.
    myDB = db.DBConnection()
    track_count = myDB.select('SELECT COUNT(*) as count FROM tracks WHERE AlbumID=?', [album_id])[0]['count']
    return track_count


# THIS IS KIND OF A MESS AND PROBABLY NEEDS TO BE CLEANED UP


def preprocess(resultlist):
    for result in resultlist:
        headers = {'User-Agent': USER_AGENT}

        if result.kind == 'soulseek':
            return True, result

        if result.kind == 'torrent':

            # rutracker always needs the torrent data
            if result.provider == 'rutracker.org':
                return ruobj.get_torrent_data(result.url), result

            # Torznab sometimes redirects
            if result.provider.startswith("Torznab") or 'torznab' in result.provider.lower():
                r = request.request_response(url=result.url, headers=headers, allow_redirects=False)
                if r:
                    link = r.headers.get('Location')
                    if link and link != result.url:
                        if link.startswith('magnet:'):
                            result = Result(
                                result.title,
                                result.size,
                                link,
                                result.provider,
                                "magnet",
                                result.matches
                            )
                            return "d10:magnet-uri%d:%se" % (len(link), link), result
                        else:
                            result = Result(
                                result.title,
                                result.size,
                                link,
                                result.provider,
                                result.kind,
                                result.matches
                            )
                            return True, result
                    else:
                        return r.content, result

            # Get out of here if we're using Transmission or Deluge
            # if not a magnet link still need the .torrent to generate hash... uTorrent support labeling
            if headphones.CONFIG.TORRENT_DOWNLOADER in [1, 3]:
                return True, result

            # Get out of here if it's a magnet link
            if result.url.lower().startswith("magnet:"):
                return True, result

            # Download the torrent file

            if result.provider in ["The Pirate Bay"]:
                headers = {
                    'User-Agent':
                        'Mozilla/5.0 (Windows NT 6.3; Win64; x64) \
                        AppleWebKit/537.36 (KHTML, like Gecko) \
                        Chrome/41.0.2243.2 Safari/537.36'
                }

            return request.request_content(url=result.url, headers=headers), result

        elif result.kind == 'magnet':
            magnet_link = result.url
            return "d10:magnet-uri%d:%se" % (len(magnet_link), magnet_link), result

        elif result.kind == 'bandcamp':
            return True, result

        else:
            if result.provider == 'headphones':
                return request.request_content(
                    url=result.url,
                    headers=headers,
                    auth=(headphones.CONFIG.HPUSER, headphones.CONFIG.HPPASS)
                    ), result
            else:
                return request.request_content(url=result.url, headers=headers), result
