"""Downloads Google Docs, converts them to Markdown for Hugo.

This script fetches Google Docs from a specified Drive folder, converts them
to Markdown, processes frontmatter, converts embedded base64 PNG images to
AVIF, and saves the results to a local directory for use with Hugo. It uses
modification times for caching and runs processing tasks in parallel.
"""
import sys
import base64
import concurrent.futures
import io
import json
import logging # Use logging module
import os
import re
import tempfile
import time
from datetime import datetime, date # Import date
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional, Tuple, Callable, Literal

# Third-party imports for date parsing
from dateutil import parser as dateutil_parser
from dateutil.tz import tzutc # Import tzutc for timezone handling

# Third-party imports
import frontmatter
import google.auth
import httplib2
from PIL import Image, UnidentifiedImageError # Pillow library for image processing
# import pillow_avif # No longer explicitly needed if Pillow >= 10.0.0 handles AVIF well
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build, Resource
from googleapiclient.errors import HttpError
from googleapiclient.http import HttpRequest, MediaIoBaseDownload

# --- Logging Setup ---
# Configure logging to output informational messages and above
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(processName)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# --- Constants and Configuration ---
MAX_RETRIES: int = 3
INITIAL_BACKOFF: float = 1.0
OUTPUT_SUBDIR: str = 'content/posts/google-drive'
DEFAULT_TIMEZONE: str = 'Asia/Tokyo'
IMAGE_WIDTH: int = 800
IMAGE_QUALITY: int = 50
IMAGE_FORMAT: str = 'avif'

# Google Drive MIME Types
MIME_TYPE_FOLDER = 'application/vnd.google-apps.folder'
MIME_TYPE_DOCUMENT = 'application/vnd.google-apps.document'

# Type alias for Drive file metadata dictionary
DriveMetadata = Dict[str, Any]

# Define possible status strings for clarity using Literal type hint
ProcessStatus = Literal[
    'skipped', 'success', 'init_error', 'download_error',
    'process_error', 'save_error', 'unknown_error'
]


class GoogleDriveClient:
    """Handles interactions with the Google Drive API.

    Provides methods for authenticating, listing files, downloading documents,
    and handling API retries. Ensures thread/process safety using requestBuilder.

    Attributes:
        service: An authenticated Google Drive API service instance.
    """

    def __init__(self) -> None:
        """Initializes the GoogleDriveClient."""
        # Store credentials and project ID obtained via ADC.
        # These are used by the requestBuilder for each API call.
        self.credentials, self.project = self._get_credentials()
        if not self.credentials:
            raise RuntimeError('Failed to obtain Google Cloud credentials.')

        # Build the service using the requestBuilder for safety
        self.service: Optional[Resource] = self._build_service()
        if not self.service:
            raise RuntimeError(
                'Failed to build Google Drive service with obtained credentials.'
            )

    def _get_credentials(self) -> Tuple[Optional[google.auth.credentials.Credentials], Optional[str]]:
        """Gets credentials using Application Default Credentials (ADC)."""
        try:
            credentials, project = google.auth.default(
                scopes=['https://www.googleapis.com/auth/drive.readonly']
            )
            logger.info(f"Successfully obtained credentials for project: {project or 'Default'}")
            return credentials, project
        except google.auth.exceptions.DefaultCredentialsError as e:
            logger.critical(f'Error getting default credentials: {e}')
            logger.critical(
                'Ensure Application Default Credentials are configured '
                '(e.g., GOOGLE_APPLICATION_CREDENTIALS env var is set by '
                'google-github-actions/auth).'
            )
            return None, None
        except Exception as e:
            logger.critical(f'Unexpected error getting default credentials: {e}')
            return None, None

    def _build_request(
        self, http: httplib2.Http, *args: Any, **kwargs: Any
    ) -> HttpRequest:
        """Creates a new AuthorizedHttp object for each request (thread/process safe).

        This method is passed to `googleapiclient.discovery.build` as the
        `requestBuilder`. It ensures that each API request uses a fresh,
        authorized `httplib2.Http` instance based on the stored credentials.

        Args:
            http: The original http object (ignored).
            *args: Positional arguments for HttpRequest.
            **kwargs: Keyword arguments for HttpRequest.

        Returns:
            A configured HttpRequest object.
        """
        if not self.credentials:
             # This check should ideally not be hit if __init__ succeeded.
             raise RuntimeError("Credentials not available for building request.")
        # Create a new AuthorizedHttp with a fresh httplib2.Http instance.
        # This is crucial for thread/process safety, ensuring each request
        # has its own isolated HTTP client authorized with the shared credentials.
        new_http = AuthorizedHttp(self.credentials, http=httplib2.Http())
        return HttpRequest(new_http, *args, **kwargs)

    def _build_service(self) -> Optional[Resource]:
        """Builds and returns the Drive v3 service object using requestBuilder."""
        if not self.credentials:
             logger.critical("Cannot build service without credentials.")
             return None
        try:
            # Use requestBuilder to handle http object creation per request
            service: Resource = build(
                'drive', 'v3',
                requestBuilder=self._build_request, # For thread/process safety
                credentials=self.credentials, # Credentials for the builder
                # Disable discovery cache to avoid issues in environments where
                # the cache might be stale or not writable (e.g. serverless).
                cache_discovery=False
            )
            logger.info('Google Drive service built successfully.')
            return service
        except Exception as e:
            logger.critical(f'Error building Drive service: {e}')
            return None

    def _execute_with_retry(
        self, func: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Any:
        """Executes a function with retry logic for specific HttpErrors."""
        retries = 0
        backoff = INITIAL_BACKOFF
        while retries < MAX_RETRIES:
            try:
                # The function `func` (e.g., _list_page, _download_all_chunks)
                # will internally use the service object which uses the requestBuilder.
                return func(*args, **kwargs)
            except HttpError as error:
                status_code = getattr(getattr(error, 'resp', None), 'status', None)
                is_retryable = status_code in [403, 429, 500, 503]

                if is_retryable and retries < MAX_RETRIES - 1:
                    retries += 1
                    logger.warning(
                        f'API Error ({status_code}): Retrying {func.__name__} '
                        f'in {backoff:.1f}s... (Attempt {retries}/{MAX_RETRIES})'
                    )
                    time.sleep(backoff)
                    backoff *= 2
                else:
                    error_type = ('Max retries reached' if is_retryable
                                  else 'Non-retryable error')
                    logger.error(
                        f"API Error ({status_code or 'Unknown'}): {error_type} "
                        f'for {func.__name__}.'
                    )
                    raise error
            except Exception as e:
                logger.exception(f'Unexpected error during {func.__name__} execution.')
                raise e

    def list_google_docs(self, folder_id: str) -> List[DriveMetadata]:
        """Recursively lists all Google Docs within a given folder ID."""
        all_files: List[DriveMetadata] = []
        page_token: Optional[str] = None
        logger.info(f'Fetching file list from Google Drive folder ID: {folder_id}')

        def _list_page(token: Optional[str]) -> Dict[str, Any]:
            """Helper function to fetch a single page of file results."""
            if not self.service:
                raise RuntimeError('Drive service is not initialized.')
            # This execute() call will use the requestBuilder
            return self.service.files().list(
                q=f"'{folder_id}' in parents and trashed = false",
                spaces='drive',
                fields='nextPageToken, files(id, name, mimeType, createdTime, modifiedTime)',
                pageToken=token
            ).execute()

        while True:
            try:
                response = self._execute_with_retry(_list_page, page_token)
                items: List[DriveMetadata] = response.get('files', [])

                for item in items:
                    mime_type = item.get('mimeType')
                    item_id = item.get('id')
                    actual_name_for_debug_only = item.get('name') # Store actual name for debugging, NOT for general logging

                    if mime_type == MIME_TYPE_FOLDER:
                        logger.info(f'Scanning subfolder with ID: {item_id if item_id else "UNKNOWN_FOLDER_ID"}')
                        if item_id:
                            try:
                                # Recursive call uses the same client instance
                                all_files.extend(self.list_google_docs(item_id))
                            except Exception as sub_error:
                                logger.error(
                                    f"Error scanning subfolder with ID {item_id}: "
                                    f'{sub_error}. Skipping folder.'
                                )
                        else:
                            # This case means item_id is None or empty.
                            # We avoid logging actual_name_for_debug_only here to prevent accidental name leakage.
                            logger.warning(f"Folder found with missing ID. Skipping exploration of this folder.")

                    elif mime_type == MIME_TYPE_DOCUMENT:
                        all_files.append(item)

                page_token = response.get('nextPageToken', None)
                if page_token is None:
                    break

            except HttpError as error:
                logger.error(
                    f'Failed to list files in folder {folder_id} after '
                    f'retries: {error}'
                )
                break
            except Exception as e:
                logger.exception(f'Unexpected error listing files in folder {folder_id}')
                break

        logger.info(
            f'Found {len(all_files)} Google Docs in folder {folder_id} and its '
            f'subfolders (list might be incomplete if errors occurred).'
        )
        return all_files

    def download_markdown(self, file_id: str, file_name: str) -> Optional[str]: # file_name is primarily for logging context here
        """Downloads a Google Doc as Markdown, handling retries.

        Args:
            file_id: The Google Drive file ID.
            file_name: The name of the file, used for logging context if errors occur.

        Returns:
            The Markdown content as a string, or None if download fails.
        """
        # Uses self.service which is built with requestBuilder
        # file_name parameter is kept for potential future use (e.g. if Google API changes)
        # or for very specific, non-standard-log debugging, but not used in standard logs.
        logger.info(f'Attempting download for file ID: {file_id}')
        if not self.service:
            logger.error(f'Drive service not initialized for download of file ID: {file_id}.')
            return None
        try:
            # This export_media call will use the requestBuilder
            request = self.service.files().export_media(
                fileId=file_id, mimeType='text/markdown'
            )
            fh = io.BytesIO()
            # The http object used by MediaIoBaseDownload is derived from the
            # service object, which uses the requestBuilder.
            downloader = MediaIoBaseDownload(
                fh, request, chunksize=10 * 1024 * 1024
            )

            def _download_all_chunks() -> io.BytesIO:
                """Helper function to download all chunks for the file."""
                done = False
                while not done:
                    status, done = downloader.next_chunk(num_retries=0)
                return fh

            downloaded_fh = self._execute_with_retry(_download_all_chunks)
            logger.info(f'Successfully downloaded file ID: {file_id}')
            return downloaded_fh.getvalue().decode('utf-8')

        except (HttpError, Exception) as error:
            logger.error(
                f'Failed to download file ID: {file_id} after retries: {error}'
            )
            return None


class MarkdownProcessor:
    """
    Handles processing of Markdown content downloaded from Google Docs.

    This includes parsing and updating frontmatter, converting embedded images
    to AVIF, and preparing the content for saving.
    """

    def __init__(self, output_dir: str) -> None:
        """Initializes the MarkdownProcessor."""
        self.output_dir: Path = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.tokyo_tz: ZoneInfo = ZoneInfo(DEFAULT_TIMEZONE)
        except Exception as e:
            logger.warning(f"Could not load timezone '{DEFAULT_TIMEZONE}'. "
                           f"Using UTC as fallback. Error: {e}")
            self.tokyo_tz = ZoneInfo('UTC')

    def get_local_path(self, file_id: str) -> Path:
        """
        Generates the local file path for a given Google Drive file ID.

        Args:
            file_id: The Google Drive file ID.

        Returns:
            The Path object for the local Markdown file.
        """
        return self.output_dir / f'{file_id}.md'

    def check_cache(
        self, file_id: str, drive_modified_time_str: Optional[str]
    ) -> Tuple[bool, bool]:
        """Checks if the local file cache is up-to-date.

        Returns:
            Tuple[bool, bool]: (should_skip, is_draft_if_skipped)
                               is_draft_if_skipped is True if the skipped file is a draft,
                               False otherwise or if not skipped.
        """
        local_path = self.get_local_path(file_id)
        logger.info(f"[{file_id}] check_cache: Local path: {local_path}, Exists: {local_path.exists()}")
        logger.info(f"[{file_id}] check_cache: Drive modifiedTime (str): '{drive_modified_time_str}' (type: {type(drive_modified_time_str)})")

        if not drive_modified_time_str:
            logger.warning(f'[{file_id}] Drive modifiedTime missing. Forcing update.')
            return False, False # Do not skip, is_draft is irrelevant here

        if local_path.exists():
            try:
                with open(local_path, 'r', encoding='utf-8') as f:
                    local_post = frontmatter.load(f)
                local_modified_time_str = local_post.get('modifiedTime')
                # Determine is_draft early, as it's needed if we return early from datetime comparison
                is_draft = local_post.get('draft', False)
                logger.info(f"[{file_id}] check_cache: Local modifiedTime from frontmatter (str): '{local_modified_time_str}' (type: {type(local_modified_time_str)})")
                logger.info(f"[{file_id}] check_cache: Local draft status: {is_draft}")

                # Datetime comparison logic
                logger.info(f"[{file_id}] check_cache: Attempting datetime comparison for modifiedTimes.")
                try:
                    if local_modified_time_str and drive_modified_time_str: # Ensure neither is None or empty
                        local_dt = dateutil_parser.isoparse(local_modified_time_str)
                        drive_dt = dateutil_parser.isoparse(drive_modified_time_str)

                        # Ensure timezone awareness for comparison, assuming UTC if naive
                        # Drive times are 'Z', local should also be ISO with offset or 'Z'
                        if local_dt.tzinfo is None or local_dt.tzinfo.utcoffset(local_dt) is None:
                            logger.warning(f"[{file_id}] check_cache: Local datetime '{local_modified_time_str}' is naive, assuming UTC for comparison.")
                            local_dt = local_dt.replace(tzinfo=tzutc()) # Make it UTC-aware
                        if drive_dt.tzinfo is None or drive_dt.tzinfo.utcoffset(drive_dt) is None:
                            # This case should be less common for Drive times if they are proper ISO
                            logger.warning(f"[{file_id}] check_cache: Drive datetime '{drive_modified_time_str}' is naive, assuming UTC for comparison.")
                            drive_dt = drive_dt.replace(tzinfo=tzutc())

                        if local_dt == drive_dt:
                            logger.info(f"[{file_id}] check_cache: Datetime comparison matches: Local ({local_dt}) == Drive ({drive_dt}). Skipping.")
                            return True, is_draft
                        else:
                            logger.info(f"[{file_id}] check_cache: Datetime comparison mismatch: Local ({local_dt}) != Drive ({drive_dt}). Proceeding to string comparison as fallback/verification.")
                    elif not local_modified_time_str:
                        logger.info(f"[{file_id}] check_cache: Local modifiedTime is missing or empty. Skipping datetime comparison.")
                    # drive_modified_time_str being None/empty is handled by the initial check in the function

                except Exception as e_parse:
                    logger.warning(f"[{file_id}] check_cache: Datetime parsing/comparison failed: {e_parse}. Falling back to string comparison.")

                # Fallback to string comparison
                comparison_result = (local_modified_time_str == drive_modified_time_str)
                logger.info(f"[{file_id}] check_cache: String comparison result (local == drive): {comparison_result}")

                if (local_modified_time_str and comparison_result): # Check local_modified_time_str again in case it was None
                    logger.info(f"[{file_id}] Skipping: Local 'modifiedTime' (string) matches Drive's.")
                    return True, is_draft
                elif not local_modified_time_str: # This condition might have been logged above if datetime comparison was skipped
                    logger.warning(
                        f"[{file_id}] Local 'modifiedTime' not found in {local_path} (checked after datetime attempt). Forcing update."
                    )
                else: # Exists but does not match (either by datetime or string)
                    logger.info( # Changed to info as mismatch is expected if datetimes didn't match
                        f"[{file_id}] Local 'modifiedTime' ('{local_modified_time_str}') does not match Drive's ('{drive_modified_time_str}') after all checks. Forcing update."
                    )
            except Exception as e: # General exception for reading local file
                logger.warning(
                    f'[{file_id}] Error reading or parsing local file {local_path} (outer try-except): {e}. '
                    f'Forcing update.'
                )
        else:
            logger.info(f"[{file_id}] Local file {local_path} does not exist. Forcing update.")
        return False, False # Do not skip, is_draft is irrelevant here

    def _convert_image(self, base64_img_data: str) -> str:
        """
        Converts a single base64 PNG image string to the target format (AVIF).

        Relies on Pillow's built-in AVIF support (Pillow >= 10.0.0).
        Note: `pillow-avif-plugin` might need to be removed from `pyproject.toml`
        if native Pillow support proves sufficient.
        If Pillow cannot handle the conversion, the original base64 string is returned.

        Args:
            base64_img_data: The base64 encoded PNG image string.

        Returns:
            The base64 encoded AVIF image string, or the original string on error.
        """
        expected_prefix = 'data:image/png;base64,'
        if not base64_img_data.startswith(expected_prefix):
            logger.warning('Image data lacks expected PNG base64 prefix. Skipping.')
            return base64_img_data

        try:
            img_part = base64_img_data.removeprefix(expected_prefix)
            img_binary = base64.b64decode(img_part)
            img = Image.open(io.BytesIO(img_binary))

            if img.mode == 'RGBA':
                bg = Image.new('RGB', img.size, (255, 255, 255))
                alpha = img.split()[-1]
                bg.paste(img, mask=alpha)
                img = bg
            elif img.mode != 'RGB':
                img = img.convert('RGB')

            if img.width > IMAGE_WIDTH:
                aspect_ratio = img.height / img.width
                new_height = round(IMAGE_WIDTH * aspect_ratio)
                logger.info(f'Resizing image from {img.width}x{img.height} to '
                            f'{IMAGE_WIDTH}x{new_height}')
                img = img.resize((IMAGE_WIDTH, new_height), Image.Resampling.LANCZOS)

            output_buffer = io.BytesIO()
            img.save(
                output_buffer,
                format=IMAGE_FORMAT,
                quality=IMAGE_QUALITY
            )
            output_buffer.seek(0)
            encoded_new_format = base64.b64encode(
                output_buffer.read()
            ).decode('utf-8')
            return f'data:image/{IMAGE_FORMAT};base64,{encoded_new_format}'

        except UnidentifiedImageError: # More specific exception from Pillow
            logger.error('Could not identify image format (Pillow UnidentifiedImageError). Skipping conversion.')
            return base64_img_data
        except Exception as e:
            logger.exception('Error converting image. Skipping conversion.')
            return base64_img_data

    def _process_images(self, content: str) -> str:
        """Finds and converts all base64 PNG images within Markdown content."""
        image_count = 0

        ref_pattern = r'^(\[image[0-9]+\]: <)(data:image/png;base64,[^>]+)(>)$'
        def replace_ref_image(match: re.Match) -> str:
            nonlocal image_count
            image_count += 1
            return match.group(1) + self._convert_image(match.group(2)) + match.group(3)
        content = re.sub(ref_pattern, replace_ref_image, content, flags=re.MULTILINE)

        inline_pattern = r'(!\[.*?\]\()(data:image/png;base64,[^\)]+)(\))'
        def replace_inline_image(match: re.Match) -> str:
            nonlocal image_count
            image_count += 1
            return match.group(1) + self._convert_image(match.group(2)) + match.group(3)
        content = re.sub(inline_pattern, replace_inline_image, content)

        if image_count > 0:
            logger.info(f'Processed {image_count} embedded PNG images.')
        return content

    def _process_shortcodes(self, content: str) -> str:
        """
        Finds and unescapes Hugo shortcodes that were escaped by Google Docs.
        """
        # This pattern looks for the escaped version of {{< ... >}}
        # It assumes that {{< ... >}} are backquote-escaped like `{{< ... >}}`.
        # The content of the shortcode is captured non-greedily.
        escaped_shortcode_pattern = r'\`(\{\{\<.*?\>\}\})\`'

        def unescape_func(match: re.Match) -> str:
            # The captured group is the inner content of the shortcode.
            inner_content = match.group(1)
            # Reconstruct the correct shortcode syntax.
            return inner_content

        processed_content, count = re.subn(escaped_shortcode_pattern, unescape_func, content)

        if count > 0:
            logger.info(f'Unescaped {count} Hugo shortcodes.')

        return processed_content

    def _parse_iso_datetime(self, iso_str: Optional[str]) -> Optional[datetime]:
        """
        Safely parses an ISO 8601 string (typically from Drive API)
        into a timezone-aware datetime object in the target timezone.

        Args:
            iso_str: The ISO 8601 datetime string.

        Returns:
            A timezone-aware datetime object, or None if parsing fails.
        """
        if not iso_str: return None
        try:
            # Use dateutil parser for flexibility, handles 'Z' (UTC) automatically.
            dt_parsed = dateutil_parser.isoparse(iso_str)

            # If the parsed datetime is naive (no timezone info),
            # ISO 8601 implies it should be treated as local time. However,
            # Drive API times are typically UTC ('Z'). For robustness, if we
            # encounter a naive datetime after parsing an ISO string that should
            # ideally have timezone info, we assume UTC. tzutc() provides UTC.
            if dt_parsed.tzinfo is None or dt_parsed.tzinfo.utcoffset(dt_parsed) is None:
                 logger.warning(f"Parsed ISO datetime '{iso_str}' is naive. Assuming UTC for safety.")
                 dt_parsed = dt_parsed.replace(tzinfo=tzutc()) # Make it UTC-aware

            # Convert the (now UTC-aware) datetime to the target timezone (e.g., self.tokyo_tz).
            return dt_parsed.astimezone(self.tokyo_tz)
        except (ValueError, TypeError) as e:
            logger.warning(f"Could not parse ISO datetime string '{iso_str}' using dateutil.isoparse: {e}")
            return None

    def _format_datetime(self, dt_obj: Optional[datetime]) -> Optional[str]:
        """
        Formats a datetime object into the Hugo-compatible string format.

        Ensures the datetime is timezone-aware (using default timezone if naive)
        before formatting.

        Args:
            dt_obj: The datetime object to format.

        Returns:
            A string representation of the datetime suitable for Hugo frontmatter,
            or None if the input is not a valid datetime object.
        """
        if not isinstance(dt_obj, datetime): return None
        try:
            dt_aware = dt_obj
            # If datetime object is naive (lacks timezone information),
            # make it aware using the configured default timezone (self.tokyo_tz).
            # This is crucial for consistent date representation in Hugo output.
            if dt_obj.tzinfo is None or dt_obj.tzinfo.utcoffset(dt_obj) is None:
                logger.warning(f'Assigning default timezone {self.tokyo_tz.key} '
                               f'to naive datetime {dt_obj} during formatting.')
                # For zoneinfo, replace(tzinfo=...) is the standard way.
                # `localize` is more for pytz when dealing with ambiguous times.
                dt_aware = dt_obj.replace(tzinfo=self.tokyo_tz)
            # Ensure it's in the target timezone before formatting if it was already aware.
            elif dt_aware.tzinfo is not self.tokyo_tz :
                dt_aware = dt_aware.astimezone(self.tokyo_tz)


            return dt_aware.strftime('%Y-%m-%d %H:%M:%S %z')
        except Exception as e:
            logger.error(f'Error formatting datetime object {dt_obj}: {e}')
            return None

    def _determine_date(
        self,
        current_date_val: Any,
        drive_created_str: Optional[str],
        file_id_for_logs: str
    ) -> datetime:
        """
        Determines the 'date' for frontmatter.

        It prioritizes existing valid 'date' from frontmatter, then falls back
        to Drive's 'createdTime', and finally to the current time if necessary.
        Ensures the resulting datetime is timezone-aware using the configured
        DEFAULT_TIMEZONE (self.tokyo_tz).

        Args:
            current_date_val: The 'date' value from existing frontmatter (can be str, date, datetime).
            drive_created_str: The 'createdTime' string from Google Drive metadata (ISO 8601 format).
            file_id_for_logs: File ID used for logging context.

        Returns:
            A timezone-aware datetime object, localized to self.tokyo_tz.
        """
        final_date_dt: Optional[datetime] = None

        if isinstance(current_date_val, datetime):
            final_date_dt = current_date_val
            # If already a datetime object, ensure it's timezone-aware.
            # If naive, assume it's intended to be in the DEFAULT_TIMEZONE.
            # If aware but different timezone, convert to DEFAULT_TIMEZONE.
            if final_date_dt.tzinfo is None or final_date_dt.tzinfo.utcoffset(final_date_dt) is None:
                logger.warning(f"[{file_id_for_logs}] Existing 'date' ({current_date_val}) is naive. "
                               f"Assigning default timezone {self.tokyo_tz.key}.")
                final_date_dt = final_date_dt.replace(tzinfo=self.tokyo_tz)
            else: # It's aware, convert to the target timezone
                final_date_dt = final_date_dt.astimezone(self.tokyo_tz)
        elif isinstance(current_date_val, date): # Handles YYYY-MM-DD from frontmatter
            logger.info(f"[{file_id_for_logs}] Frontmatter 'date' is a date object: {current_date_val}. Converting to datetime.")
            # Combine with midnight time and make it timezone-aware using DEFAULT_TIMEZONE.
            final_date_dt = datetime.combine(current_date_val, datetime.min.time()).replace(tzinfo=self.tokyo_tz)
        elif isinstance(current_date_val, str): # Handle string date values
            try:
                date_str_stripped = current_date_val.strip()
                logger.info(f"[{file_id_for_logs}] Attempting to parse 'date' string '{date_str_stripped}' using dateutil.")
                dt_parsed = dateutil_parser.parse(date_str_stripped)
                # After parsing, handle timezone: if naive, assume DEFAULT_TIMEZONE; if aware, convert.
                if dt_parsed.tzinfo is None or dt_parsed.tzinfo.utcoffset(dt_parsed) is None:
                    logger.warning(f"[{file_id_for_logs}] Parsed 'date' string '{current_date_val}' is naive. "
                                   f"Assigning default timezone {self.tokyo_tz.key}.")
                    final_date_dt = dt_parsed.replace(tzinfo=self.tokyo_tz)
                else: # Parsed date is timezone-aware, convert to DEFAULT_TIMEZONE
                    logger.info(f"[{file_id_for_logs}] Parsed 'date' string '{current_date_val}' has timezone. Converting to {self.tokyo_tz.key}.")
                    final_date_dt = dt_parsed.astimezone(self.tokyo_tz)
            except (ValueError, OverflowError, TypeError) as parse_err:
                logger.warning(f"[{file_id_for_logs}] Could not parse 'date' string '{current_date_val}': {parse_err}. "
                               f"Falling back to Drive createdTime.")
                final_date_dt = self._parse_iso_datetime(drive_created_str) # _parse_iso_datetime handles tokyo_tz
        else: # Not a datetime, date, or string, or it's None. Use Drive's createdTime.
            logger.info(f"[{file_id_for_logs}] 'date' field is not a recognized type or is missing. Falling back to Drive createdTime.")
            final_date_dt = self._parse_iso_datetime(drive_created_str) # _parse_iso_datetime handles tokyo_tz

        # Final fallback: if date is still None (e.g., Drive time was also missing/invalid), use current time.
        if not isinstance(final_date_dt, datetime):
            logger.warning(f"[{file_id_for_logs}] 'date' could not be determined from frontmatter or Drive. "
                           f"Setting to current time.")
            final_date_dt = datetime.now(self.tokyo_tz)
        return final_date_dt

    def _determine_lastmod(
        self,
        current_lastmod_val: Any,
        drive_modified_str: Optional[str],
        fallback_date_dt: datetime, # This must be a timezone-aware datetime object
        file_id_for_logs: str
    ) -> datetime:
        """
        Determines the 'lastmod' for frontmatter.

        It prioritizes existing valid 'lastmod' from frontmatter, then falls back
        to Drive's 'modifiedTime', then to the already determined 'date' (which is
        timezone-aware), and finally to the current time if necessary.
        Ensures the resulting datetime is timezone-aware using DEFAULT_TIMEZONE.

        Args:
            current_lastmod_val: The 'lastmod' value from existing frontmatter.
            drive_modified_str: The 'modifiedTime' string from Google Drive metadata (ISO 8601).
            fallback_date_dt: The determined 'date' (timezone-aware), used as a fallback.
            file_id_for_logs: File ID for logging.

        Returns:
            A timezone-aware datetime object, localized to self.tokyo_tz.
        """
        final_lastmod_dt: Optional[datetime] = None

        if isinstance(current_lastmod_val, datetime):
            final_lastmod_dt = current_lastmod_val
            if final_lastmod_dt.tzinfo is None or final_lastmod_dt.tzinfo.utcoffset(final_lastmod_dt) is None:
                logger.warning(f"[{file_id_for_logs}] Existing 'lastmod' ({current_lastmod_val}) is naive. "
                               f"Assigning default timezone {self.tokyo_tz.key}.")
                final_lastmod_dt = final_lastmod_dt.replace(tzinfo=self.tokyo_tz)
            else: # It's aware, convert to the target timezone
                final_lastmod_dt = final_lastmod_dt.astimezone(self.tokyo_tz)
        elif isinstance(current_lastmod_val, date):
            logger.info(f"[{file_id_for_logs}] Frontmatter 'lastmod' is a date object: {current_lastmod_val}. Converting to datetime.")
            final_lastmod_dt = datetime.combine(current_lastmod_val, datetime.min.time()).replace(tzinfo=self.tokyo_tz)
        elif isinstance(current_lastmod_val, str):
            try:
                lastmod_str_stripped = current_lastmod_val.strip()
                logger.info(f"[{file_id_for_logs}] Attempting to parse 'lastmod' string '{lastmod_str_stripped}' using dateutil.")
                dt_parsed = dateutil_parser.parse(lastmod_str_stripped)
                if dt_parsed.tzinfo is None or dt_parsed.tzinfo.utcoffset(dt_parsed) is None:
                    logger.warning(f"[{file_id_for_logs}] Parsed 'lastmod' string '{current_lastmod_val}' is naive. "
                                   f"Assigning default timezone {self.tokyo_tz.key}.")
                    final_lastmod_dt = dt_parsed.replace(tzinfo=self.tokyo_tz)
                else: # Parsed date is timezone-aware, convert to DEFAULT_TIMEZONE
                    logger.info(f"[{file_id_for_logs}] Parsed 'lastmod' string '{current_lastmod_val}' has timezone. Converting to {self.tokyo_tz.key}.")
                    final_lastmod_dt = dt_parsed.astimezone(self.tokyo_tz)
            except (ValueError, OverflowError, TypeError) as parse_err:
                logger.warning(f"[{file_id_for_logs}] Could not parse 'lastmod' string '{current_lastmod_val}': {parse_err}. "
                               f"Falling back to Drive modifiedTime.")
                final_lastmod_dt = self._parse_iso_datetime(drive_modified_str) # _parse_iso_datetime handles tokyo_tz
        else: # Not a datetime, date, or string, or it's None. Use Drive's modifiedTime.
            logger.info(f"[{file_id_for_logs}] 'lastmod' field is not a recognized type or is missing. Falling back to Drive modifiedTime.")
            final_lastmod_dt = self._parse_iso_datetime(drive_modified_str) # _parse_iso_datetime handles tokyo_tz

        # Fallback 1: if lastmod is still None, use the determined 'date'.
        if not isinstance(final_lastmod_dt, datetime):
            logger.warning(f"[{file_id_for_logs}] 'lastmod' could not be determined from frontmatter or Drive. "
                           f"Using determined 'date' ({fallback_date_dt}) as fallback.")
            final_lastmod_dt = fallback_date_dt # fallback_date_dt is already timezone-aware and in tokyo_tz.

        # Final fallback: if lastmod is still somehow not a datetime (should be rare), use current time.
        if not isinstance(final_lastmod_dt, datetime):
            logger.error(f"[{file_id_for_logs}] CRITICAL: 'lastmod' is still not a valid datetime after all fallbacks. Using current time.")
            final_lastmod_dt = datetime.now(self.tokyo_tz)
        return final_lastmod_dt

    def _set_other_metadata(
        self,
        post: frontmatter.Post,
        file_id: str,
        file_name: str, # This is the original file name from Drive
        drive_modified_str: Optional[str]
    ) -> None:
        """
        Sets other standard metadata fields in the frontmatter post object.

        This includes 'title' (if not already set), 'draft' (defaults to False
        if not set), 'google_drive_id', and 'modifiedTime' (from Drive, used for caching).
        It also clears any pre-existing 'conversion_error' field.

        Args:
            post: The frontmatter.Post object to modify.
            file_id: The Google Drive file ID.
            file_name: The name of the file from Google Drive, used as a fallback for title.
            drive_modified_str: The 'modifiedTime' string from Google Drive metadata.
                                This is stored directly for cache comparison purposes.
        """
        if not post.get('title'): # Only set title if it's not in the frontmatter
            post.metadata['title'] = file_name
        if 'draft' not in post.metadata: # Default 'draft' to False if not specified
            post.metadata['draft'] = False
        post.metadata['google_drive_id'] = file_id
        # Store the raw 'modifiedTime' from Drive. This is crucial for the caching logic
        # in `check_cache` to compare against the version on Drive.
        logger.info(f"[{file_id}] _set_other_metadata: Storing Drive 'modifiedTime' ({drive_modified_str}) for cache key.")
        post.metadata['modifiedTime'] = drive_modified_str
        post.metadata.pop('conversion_error', None) # Clear any previous errors

    def process_content(
        self, md_content: str, drive_metadata: DriveMetadata
    ) -> Optional[str]:
        """
        Parses Markdown content, updates its frontmatter, and converts images.

        Key operations:
        - Loads frontmatter from the Markdown string.
        - Sets or updates 'date', 'lastmod', 'title', 'draft', 'google_drive_id',
          and 'modifiedTime' in the frontmatter.
        - Converts embedded PNG images to AVIF format.
        - Handles potential errors during parsing and processing, adding a
          'conversion_error' field to frontmatter if issues occur.
        - Unescapes blockquotes at the beginning of lines.

        Args:
            md_content: The raw Markdown content string.
            drive_metadata: Metadata dictionary for the Google Drive file.

        Returns:
            The processed Markdown content string with updated frontmatter,
            or None if critical parsing errors occur.
        """
        # Use descriptive names for logging and metadata population
        file_name_for_metadata = drive_metadata.get('name', 'Unknown File')
        file_id_for_logs = drive_metadata.get('id', 'Unknown ID')

        try:
            post = frontmatter.loads(md_content)
        except Exception as parse_error:
            logger.critical(
                f"[{file_id_for_logs}] Failed to parse frontmatter from downloaded content. "
                f'Skipping all processing for this file. Error: {parse_error}'
            )
            return None # Critical error, cannot proceed with this file

        drive_created_str = drive_metadata.get('createdTime')
        drive_modified_str = drive_metadata.get('modifiedTime')

        # Determine and set date-related frontmatter.
        # These methods return timezone-aware datetime objects (localized to self.tokyo_tz).
        # They are stored as datetime objects in post.metadata initially.
        # They will be formatted to strings just before dumping the frontmatter.
        post.metadata['date'] = self._determine_date(
            post.get('date'), drive_created_str, file_id_for_logs
        )
        post.metadata['lastmod'] = self._determine_lastmod(
            post.get('lastmod'), drive_modified_str, post.metadata['date'], file_id_for_logs
        )

        # Set other non-date metadata fields (title, draft, google_drive_id, modifiedTime for cache)
        self._set_other_metadata(
            post, file_id_for_logs, file_name_for_metadata, drive_modified_str
        )

        # Process images (convert to AVIF, resize)
        try:
            post.content = self._process_images(post.content)
        except Exception as img_err:
            logger.exception(f"[{file_id_for_logs}] Error during image processing.")
            # Record error in frontmatter but continue processing if possible,
            # as text content might still be valuable.
            post.metadata['conversion_error'] = f'Image processing error: {img_err}'

        # Replace escaped blockquotes (e.g., "\> quote" to "> quote")
        # that Google Docs sometimes exports.
        try:
            logger.info(f"[{file_id_for_logs}] Replacing escaped blockquotes '^\\> ' with '> '.")
            post.content = re.sub(r'^\\> ', '> ', post.content, flags=re.MULTILINE)
        except Exception as e:
            logger.exception(f"[{file_id_for_logs}] Error replacing escaped blockquotes.")
            current_error = post.metadata.get('conversion_error', '')
            post.metadata['conversion_error'] = f"{current_error}; Blockquote unescaping error: {e}".strip('; ')


        # Unescape Hugo shortcodes that may have been escaped by Google Docs
        try:
            logger.info(f"[{file_id_for_logs}] Unescaping Hugo shortcodes.")
            post.content = self._process_shortcodes(post.content)
        except Exception as sc_err:
            logger.exception(f"[{file_id_for_logs}] Error during shortcode unescaping.")
            current_error = post.metadata.get('conversion_error', '')
            post.metadata['conversion_error'] = f"{current_error}; Shortcode unescaping error: {sc_err}".strip('; ')


        # Finalize: Format datetime objects to strings for YAML serialization and dump frontmatter
        try:
            # Convert 'date' and 'lastmod' (which are datetime objects) to formatted strings.
            post.metadata['date'] = self._format_datetime(post.metadata.get('date'))
            post.metadata['lastmod'] = self._format_datetime(post.metadata.get('lastmod'))

            # Remove any metadata fields that ended up as None (e.g., if date formatting failed).
            # This keeps the YAML clean.
            post.metadata = {k: v for k, v in post.metadata.items() if v is not None}

            logger.info(f"[{file_id_for_logs}] Metadata before final dump: {post.metadata}")
            return frontmatter.dumps(post)

        except Exception as dump_error:
            logger.critical(
                f"[{file_id_for_logs}] Failed to dump final frontmatter and content. Error: {dump_error}"
            )
            # If dumping fails, create a minimal frontmatter with the error.
            # This ensures the file still contains some context if it's created/overwritten.
            error_fm = {
                'title': post.metadata.get('title', f"Error Processing {file_id_for_logs}"),
                'google_drive_id': file_id_for_logs,
                'modifiedTime': drive_modified_str or 'Unknown', # For cache check consistency
                'conversion_error': f'CRITICAL DUMP ERROR: {dump_error}'
            }
            # Use a new Frontmatter.Post object for error content to avoid issues
            # with the original 'post' object's state which might be complex.
            error_post = frontmatter.Post(content=post.content, **error_fm) # Preserve original content if possible
            return frontmatter.dumps(error_post)

    def save_markdown(self, file_id: str, content: str) -> bool:
        """Saves the final markdown content to the local file system."""
        local_path = self.get_local_path(file_id)
        try:
            with open(local_path, 'w', encoding='utf-8') as f:
                f.write(content)
            logger.info(f'Successfully saved: {local_path}')
            return True
        except IOError as e:
            logger.error(f'Error writing file {local_path}: {e}')
            return False
        except Exception as e:
            logger.exception(f'An unexpected error occurred saving {file_id}')
            return False

# --- Parallel Task Function ---

def process_single_file_task(
    drive_metadata: DriveMetadata, output_dir_str: str
) -> Tuple[ProcessStatus, bool]:
    """
    Processes a single Google Drive file.

    This function is designed to be run in a separate process by a
    ProcessPoolExecutor. It handles initialization of clients, cache checking,
    downloading, content processing, and saving for a single file.

    Args:
        drive_metadata: Metadata of the Google Drive file to process.
        output_dir_str: The absolute path to the output directory as a string.

    Returns:
        A tuple containing:
            - ProcessStatus: A string indicating the outcome of the processing
                             (e.g., 'success', 'skipped', 'download_error').
            - bool: True if the processed file is a draft, False otherwise.
                    Defaults to True if an error occurs before draft status
                    can be determined.
    """
    file_id = drive_metadata.get('id')
    drive_modified_time = drive_metadata.get('modifiedTime')

    if not file_id:
        logger.error("File metadata missing 'id'. Cannot process.")
        return 'process_error', True # Default to draft on error

    logger.info(f'Starting processing for file ID: {file_id}')

    try:
        processor = MarkdownProcessor(output_dir_str)
        client = GoogleDriveClient()
    except Exception as init_error:
        logger.exception(f'Error initializing client/processor for file ID {file_id}')
        return 'init_error', True # Default to draft on error

    try:
        should_skip, is_draft_if_skipped = processor.check_cache(file_id, drive_modified_time)
        if should_skip:
            return 'skipped', is_draft_if_skipped

        file_name_for_download = drive_metadata.get('name', f"ID: {file_id or 'Unknown'}")
        md_content = client.download_markdown(file_id, file_name_for_download)
        if md_content is None:
            return 'download_error', True # Default to draft on error

        processed_content_str = processor.process_content(md_content, drive_metadata)
        if processed_content_str is None:
            return 'process_error', True # Default to draft on error

        # Get draft status from processed content
        is_draft: bool = True # Default to draft if parsing fails
        try:
            processed_post_obj = frontmatter.loads(processed_content_str)
            # If 'draft' is not present, it's not a draft by default in Hugo.
            # However, for our logic, if not specified, assume False (not a draft)
            # unless explicitly set to True.
            is_draft = processed_post_obj.get('draft', False)
        except Exception:
            logger.warning(
                f"[{file_id}] Could not parse frontmatter from processed content "
                f"to get draft status. Assuming draft=False for safety if processed."
            )
            # If content was processed but draft status can't be parsed from it,
            # assume it's not a draft. This is safer than assuming it IS a draft,
            # as it means content might go public rather than being hidden.
            is_draft = False

        if processor.save_markdown(file_id, processed_content_str):
            return 'success', is_draft
        else:
            return 'save_error', is_draft # Return determined draft status even on save error

    except Exception as e:
        logger.exception(f'Unexpected error during task execution for file ID {file_id}')
        return 'unknown_error', True # Default to draft on major error


# --- Helper Functions for Main ---

def _synchronize_local_files(output_path: Path, current_drive_ids: set[str]) -> int:
    """
    Synchronizes local Markdown files with the current list of Drive file IDs.

    Deletes local files that are no longer present in Google Drive.
    It also determines if any of the deleted files were 'public' (not draft).

    Args:
        output_path: The local directory where Markdown files are stored.
        current_drive_ids: A set of Google Drive file IDs that currently exist.

    Returns:
        The number of public (non-draft) files that were deleted locally.
    """
    logger.info(f'Checking local files in {output_path.resolve()} for cleanup...')
    deleted_public_files_count = 0

    if not output_path.exists():
        logger.info('Output directory does not exist, skipping cleanup.')
        return 0

    local_md_files_paths = list(output_path.glob('*.md'))
    # Stores metadata for local files: {'file_id': {'path': Path, 'is_draft': bool}}
    local_files_metadata: Dict[str, Dict[str, Any]] = {}

    for local_path_obj in local_md_files_paths:
        file_id = local_path_obj.stem
        # Basic check for Drive ID format (alphanumeric, hyphens, underscores)
        if re.match(r'^[a-zA-Z0-9_-]+$', file_id):
            is_draft = True # Default to draft if parsing frontmatter fails
            try:
                with open(local_path_obj, 'r', encoding='utf-8') as f:
                    post = frontmatter.load(f)
                    is_draft = post.get('draft', False)
                local_files_metadata[file_id] = {'path': local_path_obj, 'is_draft': is_draft}
            except Exception as e:
                logger.warning(
                    f"Could not read/parse {local_path_obj} to get draft status "
                    f"during cleanup pre-check: {e}. Assuming draft."
                )
                local_files_metadata[file_id] = {'path': local_path_obj, 'is_draft': True}
        else:
            logger.warning(
                f'Found local file with unexpected name format: '
                f'{local_path_obj.name}. Skipping cleanup consideration.'
            )

    local_file_ids_on_disk = set(local_files_metadata.keys())
    ids_to_delete_from_disk = local_file_ids_on_disk - current_drive_ids

    if ids_to_delete_from_disk:
        logger.info(f'Found {len(ids_to_delete_from_disk)} local files to delete:')
        for file_id_to_delete in ids_to_delete_from_disk:
            metadata = local_files_metadata.get(file_id_to_delete)
            if metadata:
                local_file_path_to_delete = metadata['path']
                was_public = not metadata['is_draft']
                logger.info(f'  - Deleting {local_file_path_to_delete.name} (was_public: {was_public})')
                if was_public:
                    deleted_public_files_count += 1
                try:
                    local_file_path_to_delete.unlink()
                except OSError as e:
                    logger.error(f'    Error deleting file {local_file_path_to_delete}: {e}')
            else:
                # This case should ideally not be reached if logic is correct.
                logger.warning(
                    f"Attempted to delete file ID {file_id_to_delete} but its "
                    f"metadata was not pre-scanned. Skipping deletion."
                )
    else:
        logger.info('No local files need deletion.')
    return deleted_public_files_count


def _handle_marker_file(public_content_changed: bool, deleted_public_files_count: int) -> None:
    """
    Creates or removes a .content-updated marker file.

    This marker file is used by downstream processes (e.g., a deployment script)
    to determine if there were changes to public content that warrant a new deployment.

    Args:
        public_content_changed: True if any non-draft content was successfully
                                updated or created during the main processing.
        deleted_public_files_count: The number of public files deleted during local sync.
    """
    # Marker creation condition:
    # Deploy if any non-draft content was successfully updated/created OR if any public file was deleted.
    should_create_marker = public_content_changed or (deleted_public_files_count > 0)
    marker_file = Path('.content-updated')

    logger.info(
        f"Marker logic: public_content_changed: {public_content_changed}, "
        f"deleted_public_files_count: {deleted_public_files_count}, "
        f"should_create_marker: {should_create_marker}"
    )

    if should_create_marker:
        logger.info("Public content updated or public file deleted. Creating marker file '.content-updated'...")
        try:
            marker_file.touch()
            if marker_file.exists():
                 logger.info("Successfully created marker file.")
            else:
                 # This state (touched but not existing) is unlikely but good to log.
                 logger.error("Marker file creation attempted but file does not exist afterwards.")
        except OSError as e:
            logger.error(f"Failed to create marker file '.content-updated': {e}")
    else:
        logger.info("No public content updates or public file deletions. Marker file will not be created/will be removed if it exists.")
        if marker_file.exists():
             logger.info("Attempting to remove existing marker file as no relevant updates occurred...")
             try:
                 marker_file.unlink()
                 logger.info("Successfully removed existing marker file.")
             except OSError as e:
                 logger.warning(f"Could not remove existing marker file '.content-updated': {e}")


# --- Main Execution ---

def main() -> None:
    """
    Main function to orchestrate the Google Docs to Markdown conversion process.

    Steps:
    1. Initializes environment (reads parent folder ID, sets up output path).
    2. Initializes GoogleDriveClient.
    3. Lists all Google Docs from the specified Drive folder.
    4. Syncs local files: deletes local Markdown files that no longer exist in Drive.
    5. Processes each Drive file in parallel using ProcessPoolExecutor:
        - Checks local cache against Drive modification time.
        - If necessary, downloads the doc as Markdown.
        - Processes the Markdown (frontmatter, image conversion).
        - Saves the final Markdown to the local file system.
    6. Logs a summary of the conversion process.
    7. Creates or removes a '.content-updated' marker file based on whether
       public (non-draft) content was changed.
    8. Exits with appropriate status code (0 for success, 1 for failures).
    """
    start_time = time.time()
    logger.info(f'Starting Google Docs to Markdown conversion at {datetime.now()}...')

    parent_folder_id = os.getenv('GOOGLE_DRIVE_PARENT_ID')
    if not parent_folder_id:
        logger.critical('GOOGLE_DRIVE_PARENT_ID environment variable is not set.')
        sys.exit(1)

    logger.info(f'Target Parent Folder ID: {parent_folder_id}')
    output_path = Path(OUTPUT_SUBDIR)
    logger.info(f'Output directory: {output_path.resolve()}')

    try:
        # Initialize client for listing
        initial_client = GoogleDriveClient()
    except RuntimeError as e:
        logger.critical(f'Failed to initialize Google Drive client: {e}')
        sys.exit(1)

    drive_files: List[DriveMetadata] = initial_client.list_google_docs(parent_folder_id)
    current_drive_ids = {f['id'] for f in drive_files if f.get('id') if f.get('id')} # Ensure id is not None
    logger.info(f'Found {len(current_drive_ids)} unique file IDs in Google Drive.')

    # Synchronize local files with Google Drive and count deleted public files
    deleted_public_files_count = _synchronize_local_files(output_path, current_drive_ids)

    # Early exit if no files to process and no public files were deleted (idempotency)
    if not drive_files and deleted_public_files_count == 0:
        logger.info('No Google Docs found to process and no public files were deleted. Conversion finished.')
        logger.info('Conversion finished.')
        return

    logger.info(f'Submitting {len(drive_files)} Google Docs for processing...')

    # Parallel Processing
    results: Dict[str, int] = {
        'success': 0, 'skipped': 0, 'failed': 0, 'public_updated': 0
    }
    processed_file_details: List[Tuple[ProcessStatus, bool]] = [] # Stores (status, is_draft)
    output_dir_abs_str = str(output_path.resolve())

    with concurrent.futures.ProcessPoolExecutor() as executor:
        # output_dir_abs_str is used because Path objects may not be picklable
        # across processes, but string paths are.
        future_to_file_meta: Dict[concurrent.futures.Future[Tuple[ProcessStatus, bool]], DriveMetadata] = {
            executor.submit(process_single_file_task, meta, output_dir_abs_str): meta
            for meta in drive_files
        }

        for future in concurrent.futures.as_completed(future_to_file_meta):
            file_meta = future_to_file_meta[future]
            file_id = file_meta.get('id', 'Unknown ID')
            try:
                status, is_draft = future.result()
                processed_file_details.append((status, is_draft))

                if status == 'success':
                    results['success'] += 1
                    if not is_draft:
                        results['public_updated'] += 1
                elif status == 'skipped':
                    results['skipped'] += 1
                else: # init_error, download_error, process_error, save_error, unknown_error
                    results['failed'] += 1
                    logger.error(
                        f"Processing failed for file ID '{file_id}' "
                        f'with status: {status} (is_draft: {is_draft})'
                    )
            except Exception as exc: # pylint: disable=broad-except
                results['failed'] += 1
                # Log the exception with file_id for better debugging
                logger.exception(
                    f"File ID '{file_id}' generated an unexpected "
                    f'exception during execution: {exc}'
                )
                processed_file_details.append(('unknown_error', True)) # Assume draft on unhandled exception

    # Report Summary
    end_time = time.time()
    duration = end_time - start_time
    summary = (
        f"\n{'-'*30}\n"
        f"Conversion Summary:\n"
        f"  Duration: {duration:.2f} seconds\n"
        f"  Total Files Found: {len(drive_files)}\n"
        f"  Successfully Processed (Overall): {results['success']}\n"
        f"  Successfully Processed (Public, non-draft): {results['public_updated']}\n"
        f"  Skipped (Up-to-date): {results['skipped']}\n"
        f"  Failed: {results['failed']}\n"
        f"{'-'*30}"
    )
    logger.info(summary)

    # Exit Status
    if results['failed'] > 0:
        logger.error('Exiting with error code 1 due to processing failures.')
        sys.exit(1) # Exit with error code if there were failures
    else:
        logger.info('Conversion completed successfully.')
        # exit_code = 0 # No need to set exit_code if we sys.exit(0) later

    # --- Marker File Logic ---
    # Determine if any public (non-draft) content was processed or deleted.
    public_content_changed = results['public_updated'] > 0
    _handle_marker_file(public_content_changed, deleted_public_files_count)

    logger.info("Exiting with code: 0 (Success)")
    sys.exit(0)


if __name__ == '__main__':
    main()
