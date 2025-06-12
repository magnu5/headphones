from collections import defaultdict, namedtuple
import os
import time
import slskd_api
import headphones
from headphones import logger
from datetime import datetime, timedelta

Result = namedtuple('Result', ['title', 'size', 'user', 'provider', 'type', 'matches', 'bandwidth', 'hasFreeUploadSlot', 'queueLength', 'files', 'kind', 'url', 'folder'])

def initialize_soulseek_client():
    host = headphones.CONFIG.SOULSEEK_API_URL
    api_key = headphones.CONFIG.SOULSEEK_API_KEY
    return slskd_api.SlskdClient(host=host, api_key=api_key)

    # Search logic, calling search and processing fucntions
def search(artist, album, year, num_tracks, losslessOnly, allow_lossless, user_search_term):
    client = initialize_soulseek_client()

    # override search string with user provided search term if entered
    if user_search_term:
        artist = user_search_term
        album = ''
        year = ''
    
    # Stage 1: Search with artist, album, year, and num_tracks
    logger.info(f"Searching Soulseek using term: {artist} {album} {year}")
    results = execute_search(client, artist, album, year, losslessOnly, allow_lossless)
    processed_results = process_results(results, losslessOnly, allow_lossless, num_tracks)
    if processed_results or user_search_term or album.lower() == artist.lower():
        return processed_results
    
    # Stage 2: If Stage 1 fails, search with artist, album, and num_tracks (excluding year)
    logger.info("Soulseek search stage 1 did not meet criteria. Retrying without year...")
    results = execute_search(client, artist, album, None, losslessOnly, allow_lossless)
    processed_results = process_results(results, losslessOnly, allow_lossless, num_tracks)
    if processed_results or artist == "Various Artists":
        return processed_results
    
    # Stage 3: Final attempt, search only with artist and album
    logger.info("Soulseek search stage 2 did not meet criteria. Final attempt with only artist and album.")
    results = execute_search(client, artist, album, None, losslessOnly, allow_lossless)
    processed_results = process_results(results, losslessOnly, allow_lossless, num_tracks, ignore_track_count=True)

    return processed_results

def execute_search(client, artist, album, year, losslessOnly, allow_lossless):
    search_text = f"{artist} {album}"
    if year:
        search_text += f" {year}"

    if losslessOnly:
        search_text += " flac"
    elif not allow_lossless:
            search_text += " mp3"

    # Actual search
    search_response = client.searches.search_text(searchText=search_text, filterResponses=True)
    search_id = search_response.get('id')

    # Wait for search completion and return response
    while not client.searches.state(id=search_id).get('isComplete'):
        time.sleep(2)
    
    return client.searches.search_responses(id=search_id)

# Processing the search result passed
def process_results(results, losslessOnly, allow_lossless, num_tracks, ignore_track_count=False):

    if losslessOnly:
        valid_extensions = {'.flac'}
    elif allow_lossless:
        valid_extensions = {'.mp3', '.flac'}
    else:
        valid_extensions = {'.mp3'}

    albums = defaultdict(lambda: {'files': [], 'user': None, 'hasFreeUploadSlot': None, 'queueLength': None, 'uploadSpeed': None})

    # Extract info from the api response and combine files at album level
    for result in results:
        user = result.get('username')
        hasFreeUploadSlot = result.get('hasFreeUploadSlot')
        queueLength = result.get('queueLength')
        uploadSpeed = result.get('uploadSpeed')

        # Only handle .mp3 and .flac
        for file in result.get('files', []):
            filename = file.get('filename')
            file_extension = os.path.splitext(filename)[1].lower()
            if file_extension in valid_extensions:
                #album_directory = os.path.dirname(filename)
                album_directory = filename.rsplit('\\', 1)[0]
                albums[album_directory]['files'].append(file)

                # Update metadata only once per album_directory
                if albums[album_directory]['user'] is None:
                    albums[album_directory].update({
                        'user': user,
                        'hasFreeUploadSlot': hasFreeUploadSlot,
                        'queueLength': queueLength,
                        'uploadSpeed': uploadSpeed,
                    })

    # Filter albums based on num_tracks, add bunch of useful info to the compiled album
    final_results = []
    for directory, album_data in albums.items():
        if ignore_track_count and len(album_data['files']) > 1 or len(album_data['files']) == num_tracks:
            #album_title = os.path.basename(directory)
            album_title = directory.rsplit('\\', 1)[1]
            total_size = sum(file.get('size', 0) for file in album_data['files'])
            final_results.append(Result(
                title=album_title,
                size=int(total_size),
                user=album_data['user'],
                provider="soulseek",
                type="soulseek",
                matches=True,
                bandwidth=album_data['uploadSpeed'],
                hasFreeUploadSlot=album_data['hasFreeUploadSlot'],
                queueLength=album_data['queueLength'],
                files=album_data['files'],
                kind='soulseek',
                url='http://' + album_data['user'] + album_title, # URL is needed in other parts of the program.
                #folder=os.path.basename(directory)
                folder = album_title
            ))

    return final_results


def download(user, filelist):
    client = initialize_soulseek_client()
    client.transfers.enqueue(username=user, files=filelist)


def download_completed():
    client = initialize_soulseek_client()
    all_downloads = client.transfers.get_all_downloads(includeRemoved=False)
    album_completion_tracker = {}  # Tracks completion state of each album's songs
    album_errored_tracker = {}  # Tracks albums with errored downloads

    # Anything older than 24 hours will be canceled
    cutoff_time = datetime.now() - timedelta(hours=24)

    # Identify errored and completed albums
    for download in all_downloads:
        directories = download.get('directories', [])
        for directory in directories:
            album_part = directory.get('directory', '').split('\\')[-1]
            files = directory.get('files', [])
            for file_data in files:
                state = file_data.get('state', '')
                requested_at_str = file_data.get('requestedAt', '1900-01-01 00:00:00')
                requested_at = parse_datetime(requested_at_str)

                # Initialize or update album entry in trackers
                if album_part not in album_completion_tracker:
                    album_completion_tracker[album_part] = {'total': 0, 'completed': 0, 'errored': 0}
                if album_part not in album_errored_tracker:
                    album_errored_tracker[album_part] = False

                album_completion_tracker[album_part]['total'] += 1

                if 'Completed, Succeeded' in state:
                    album_completion_tracker[album_part]['completed'] += 1
                elif 'Completed, Errored' in state or requested_at < cutoff_time:
                    album_completion_tracker[album_part]['errored'] += 1
                    album_errored_tracker[album_part] = True  # Mark album as having errored downloads

    # Identify errored albums
    errored_albums = {album for album, errored in album_errored_tracker.items() if errored}

    # Cancel downloads for errored albums
    for download in all_downloads:
        directories = download.get('directories', [])
        for directory in directories:
            album_part = directory.get('directory', '').split('\\')[-1]
            files = directory.get('files', [])
            for file_data in files:
                if album_part in errored_albums:
                    # Extract 'id' and 'username' for each file to cancel the download
                    file_id = file_data.get('id', '')
                    username = file_data.get('username', '')
                    success = client.transfers.cancel_download(username, file_id)
                    if not success:
                        logger.debug(f"Soulseek failed to cancel download for file ID: {file_id}")

    # Clear completed/canceled/errored stuff from client downloads
    try:
        client.transfers.remove_completed_downloads()
    except Exception as e:
        logger.debug(f"Soulseek failed to remove completed downloads: {e}")

    # Identify completed albums
    completed_albums = {album for album, counts in album_completion_tracker.items() if counts['total'] == counts['completed']}

    # Return both completed and errored albums
    return completed_albums, errored_albums


def checkCompleted(username, folder_name):
    """
    Check if a Soulseek download has completed with robust error handling.
    
    Args:
        username: Username from whom the download was initiated
        folder_name: Name of the folder/album being downloaded
        
    Returns:
        dict: {'completed': bool, 'progress': float, 'status': str} or None if error
    """
    try:
        client = initialize_soulseek_client()
        if not client:
            logger.error("Failed to initialize Soulseek client")
            return None
            
        downloads = client.transfers.get_downloads(username)
        
        if downloads is None:
            logger.error(f"Soulseek API returned no download data for user {username}")
            return None
        
        # Anything older than 24 hours will be considered stale
        cutoff_time = datetime.now() - timedelta(hours=24)
        
        total_count = 0
        completed_count = 0
        errored_count = 0
        
        # Find the specific album/folder
        directories = downloads.get('directories', [])
        for directory in directories:
            try:
                album_part = directory.get('directory', '').split('\\')[-1]
                if album_part == folder_name:
                    files = directory.get('files', [])
                    for file_data in files:
                        try:
                            state = file_data.get('state', '')
                            requested_at_str = file_data.get('requestedAt', '1900-01-01 00:00:00')
                            requested_at = parse_datetime(requested_at_str)
                            
                            total_count += 1
                            
                            if 'Completed, Succeeded' in state:
                                completed_count += 1
                            elif 'Completed, Errored' in state or requested_at < cutoff_time:
                                errored_count += 1
                        except Exception as e:
                            logger.warning(f"Error processing file data in Soulseek download check: {e}")
                            errored_count += 1
                    break
            except Exception as e:
                logger.warning(f"Error processing directory in Soulseek download check: {e}")
                continue
        
        if total_count == 0:
            logger.warning(f"Soulseek download {folder_name} from {username} not found")
            return None
            
        # Calculate progress and status
        progress = completed_count / total_count if total_count > 0 else 0
        completed = completed_count == total_count and errored_count == 0
        
        if errored_count > 0:
            status = 'errored'
        elif completed:
            status = 'completed'
        else:
            status = 'downloading'
            
        logger.debug(f"Soulseek download {folder_name}: {progress*100:.1f}% complete, {completed_count}/{total_count} files, status: {status}")
        
        return {
            'completed': completed,
            'progress': progress,
            'status': status,
            'name': folder_name
        }
        
    except Exception as e:
        logger.error(f"Error checking Soulseek download completion for {folder_name}: {e}")
        return None


def download_completed_album(username, foldername):
    """Check if Soulseek album download is completed with robust error handling."""
    try:
        client = initialize_soulseek_client()
        if not client:
            logger.error("Failed to initialize Soulseek client for album status check")
            return False, True  # Assume error state on client failure
            
        downloads = client.transfers.get_downloads(username)
        
        if downloads is None:
            logger.error(f"Soulseek API returned no download data for user {username}")
            return False, True  # Assume error state on API failure

        # Anything older than 24 hours will be canceled
        cutoff_time = datetime.now() - timedelta(hours=24)

        total_count = 0
        completed_count = 0
        errored_count = 0
        file_ids = []

        # Identify errored and completed album
        try:
            directories = downloads.get('directories', [])
            for directory in directories:
                try:
                    album_part = directory.get('directory', '').split('\\')[-1]
                    if album_part == foldername:
                        files = directory.get('files', [])
                        for file_data in files:
                            try:
                                state = file_data.get('state', '')
                                requested_at_str = file_data.get('requestedAt', '1900-01-01 00:00:00')
                                requested_at = parse_datetime(requested_at_str)

                                total_count += 1
                                file_id = file_data.get('id', '')
                                file_ids.append(file_id)

                                if 'Completed, Succeeded' in state:
                                    completed_count += 1
                                elif 'Completed, Errored' in state or requested_at < cutoff_time:
                                    errored_count += 1
                            except Exception as e:
                                logger.warning(f"Error processing file data for {foldername}: {e}")
                                errored_count += 1
                        break
                except Exception as e:
                    logger.warning(f"Error processing directory for {foldername}: {e}")
                    continue
        except Exception as e:
            logger.error(f"Error accessing directories for {foldername}: {e}")
            return False, True

        completed = True if completed_count == total_count and total_count > 0 else False
        errored = True if errored_count > 0 else False

        # Cancel downloads for errored album
        if errored and file_ids:
            logger.info(f"Cancelling errored downloads for {foldername}")
            for file_id in file_ids:
                try:
                    success = client.transfers.cancel_download(username, file_id, remove=True)
                    if not success:
                        logger.debug(f"Failed to cancel download for file ID: {file_id}")
                except Exception as e:
                    logger.debug(f"Soulseek failed to cancel download for folder {foldername}, file ID {file_id}: {e}")

        logger.debug(f"Soulseek album {foldername} status: completed={completed}, errored={errored}, {completed_count}/{total_count} files")
        return completed, errored
        
    except Exception as e:
        logger.error(f"Error checking Soulseek album completion for {foldername}: {e}")
        return False, True  # Assume error state on exception


def parse_datetime(datetime_string):
    """Parse the datetime API response with error handling."""
    try:
        # Parse the datetime api response
        if '.' in datetime_string:
            datetime_string = datetime_string[:datetime_string.index('.')+7]
        return datetime.strptime(datetime_string, '%Y-%m-%dT%H:%M:%S.%f')
    except ValueError as e:
        logger.warning(f"Failed to parse datetime string '{datetime_string}': {e}")
        # Return a very old date as fallback
        return datetime(1900, 1, 1)
    except Exception as e:
        logger.error(f"Unexpected error parsing datetime '{datetime_string}': {e}")
        return datetime(1900, 1, 1)
