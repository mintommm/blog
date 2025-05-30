"""Downloads Google Docs, converts them to Markdown for Hugo.

This script fetches Google Docs from a specified Drive folder, converts them
to Markdown, processes frontmatter, converts embedded base64 PNG images to
AVIF, and saves the results to a local directory for use with Hugo. It uses
modification times for caching and runs processing tasks in parallel.
"""

import base64
import concurrent.futures
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
import PIL
import pillow_avif
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
        # Get credentials once during initialization
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
             # This should ideally not happen if __init__ succeeded
             raise RuntimeError("Credentials not available for building request.")
        # Create a new AuthorizedHttp with a fresh httplib2.Http instance
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
                requestBuilder=self._build_request,
                credentials=self.credentials, # Pass credentials for builder
                cache_discovery=False # Avoid potential discovery cache issues
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
                    item_name = item.get('name', f"Unnamed Item (ID: {item_id})") # Keep item_name for potential internal use, but don't log it directly

                    if mime_type == MIME_TYPE_FOLDER:
                        logger.info(f'Scanning subfolder with ID: {item_id}')
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
                            # item_name is still useful here for context if ID is missing
                            logger.warning(f"Folder '{item_name}' has no ID. Skipping.")

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

    def download_markdown(self, file_id: str, file_name: str) -> Optional[str]: # file_name param retained for context if needed, but not logged directly
        """Downloads a Google Doc as Markdown, handling retries."""
        # Uses self.service which is built with requestBuilder
        logger.info(f'Attempting download for file ID: {file_id}')
        if not self.service:
            logger.error('Drive service not initialized for download.')
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
    """Handles processing and saving of Markdown files derived from Google Docs."""

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
        """Generates the local file path for a given Google Drive file ID."""
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
                is_draft = local_post.get('draft', False) # Get draft status
                logger.info(f"[{file_id}] check_cache: Local modifiedTime from frontmatter (str): '{local_modified_time_str}' (type: {type(local_modified_time_str)})")
                logger.info(f"[{file_id}] check_cache: Local draft status: {is_draft}")

                comparison_result = (local_modified_time_str == drive_modified_time_str)
                logger.info(f"[{file_id}] check_cache: Comparison (local == drive): {comparison_result}")

                if (local_modified_time_str and comparison_result):
                    logger.info(f"[{file_id}] Skipping: Local 'modifiedTime' matches Drive's.")
                    return True, is_draft # Skip, return its draft status
                elif not local_modified_time_str:
                    logger.warning(
                        f"[{file_id}] Local 'modifiedTime' not found in {local_path}. Forcing update."
                    )
                else: # Exists but does not match
                    logger.warning(
                        f"[{file_id}] Local 'modifiedTime' ('{local_modified_time_str}') does not match Drive's ('{drive_modified_time_str}'). Forcing update."
                    )
            except Exception as e:
                logger.warning(
                    f'[{file_id}] Error reading or parsing local file {local_path}: {e}. '
                    f'Forcing update.'
                )
        else:
            logger.info(f"[{file_id}] Local file {local_path} does not exist. Forcing update.")
        return False, False # Do not skip, is_draft is irrelevant here

    def _convert_image(self, base64_img_data: str) -> str:
        """Converts a single base64 PNG image string to the target format (AVIF)."""
        expected_prefix = 'data:image/png;base64,'
        if not base64_img_data.startswith(expected_prefix):
            logger.warning('Image data lacks expected PNG base64 prefix. Skipping.')
            return base64_img_data

        try:
            img_part = base64_img_data.removeprefix(expected_prefix)
            img_binary = base64.b64decode(img_part)
            img = PIL.Image.open(io.BytesIO(img_binary))

            if img.mode == 'RGBA':
                bg = PIL.Image.new('RGB', img.size, (255, 255, 255))
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
                img = img.resize((IMAGE_WIDTH, new_height), PIL.Image.LANCZOS)

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

        except PIL.UnidentifiedImageError:
            logger.error('Could not identify image format. Skipping conversion.')
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

    def _parse_iso_datetime(self, iso_str: Optional[str]) -> Optional[datetime]:
        """Safely parses an ISO 8601 string (typically from Drive API)
           into a timezone-aware datetime object in the target timezone."""
        if not iso_str: return None
        try:
            # Use dateutil parser for flexibility, handles 'Z' automatically
            dt_parsed = dateutil_parser.isoparse(iso_str)
            # If naive after parsing ISO string, assume UTC as per ISO 8601
            if dt_parsed.tzinfo is None or dt_parsed.tzinfo.utcoffset(dt_parsed) is None:
                 logger.warning(f"Parsed ISO datetime '{iso_str}' is naive. Assuming UTC.")
                 dt_parsed = dt_parsed.replace(tzinfo=tzutc())
            # Convert to the target timezone
            return dt_parsed.astimezone(self.tokyo_tz)
        except (ValueError, TypeError) as e:
            logger.warning(f"Could not parse ISO datetime string '{iso_str}' using dateutil.isoparse: {e}")
            return None

    def _format_datetime(self, dt_obj: Optional[datetime]) -> Optional[str]:
        """Formats a datetime object into the Hugo-compatible string format."""
        if not isinstance(dt_obj, datetime): return None
        try:
            dt_aware = dt_obj
            if dt_obj.tzinfo is None:
                logger.warning(f'Assigning default timezone {DEFAULT_TIMEZONE} '
                               f'to naive datetime {dt_obj} during formatting.')
                dt_aware = dt_obj.replace(tzinfo=self.tokyo_tz)
            return dt_aware.strftime('%Y-%m-%d %H:%M:%S %z')
        except Exception as e:
            logger.error(f'Error formatting datetime object {dt_obj}: {e}')
            return None

    def process_content(
        self, md_content: str, drive_metadata: DriveMetadata
    ) -> Optional[str]:
        """Parses content, updates frontmatter, converts images."""
        file_name = drive_metadata.get('name', 'Unknown File') # Retain for internal logic, not direct logging
        file_id = drive_metadata.get('id', 'Unknown ID')

        try:
            post = frontmatter.loads(md_content)
        except Exception as parse_error:
            logger.critical(
                f"Failed to parse frontmatter for file ID '{file_id}'. "
                f'Skipping modification. Error: {parse_error}'
            )
            return None

        drive_created_str = drive_metadata.get('createdTime')
        drive_modified_str = drive_metadata.get('modifiedTime')

        # Determine 'date' using dateutil.parser for flexibility
        current_date_val = post.get('date')
        final_date_dt: Optional[datetime] = None
        if isinstance(current_date_val, datetime): # Handle existing datetime objects
            final_date_dt = current_date_val
            if final_date_dt.tzinfo is None: # Ensure timezone awareness
                 logger.warning(f"Existing date '{current_date_val}' is naive. "
                                f"Assigning default timezone {DEFAULT_TIMEZONE}.")
                 final_date_dt = final_date_dt.replace(tzinfo=self.tokyo_tz)
            else: # Convert to target timezone if different
                 final_date_dt = final_date_dt.astimezone(self.tokyo_tz)
        elif isinstance(current_date_val, date): # Handle date objects (from YYYY-MM-DD)
             logger.info(f"Frontmatter provided date object: {current_date_val}. Converting to datetime.")
             # Combine date with midnight time and add default timezone
             final_date_dt = datetime.combine(current_date_val, datetime.min.time()).replace(tzinfo=self.tokyo_tz)
        elif isinstance(current_date_val, str): # Handle string values
            try:
                # Use dateutil.parser.parse for flexible parsing after stripping whitespace
                date_str_stripped = current_date_val.strip()
                logger.info(f"Attempting to parse stripped date string '{date_str_stripped}' using dateutil.")
                # default=datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                # can be used if only date is provided, but let's handle timezone explicitly
                dt_parsed = dateutil_parser.parse(date_str_stripped)

                # Handle timezone: if naive, assume default; if aware, convert
                if dt_parsed.tzinfo is None or dt_parsed.tzinfo.utcoffset(dt_parsed) is None:
                    logger.warning(f"Parsed date '{current_date_val}' is naive. "
                                   f"Assigning default timezone {DEFAULT_TIMEZONE}.")
                    final_date_dt = dt_parsed.replace(tzinfo=self.tokyo_tz)
                else:
                    logger.info(f"Parsed date '{current_date_val}' has timezone. Converting to {DEFAULT_TIMEZONE}.")
                    final_date_dt = dt_parsed.astimezone(self.tokyo_tz)

            except (ValueError, OverflowError, TypeError) as parse_err:
                logger.warning(f"Could not parse existing date string '{current_date_val}' "
                               f"using dateutil: {parse_err}. Falling back to Drive createdTime.")
                final_date_dt = self._parse_iso_datetime(drive_created_str)
        else: # Not datetime or string, use Drive time
            final_date_dt = self._parse_iso_datetime(drive_created_str)

        # Fallback if date is still None after all attempts
        if not isinstance(final_date_dt, datetime):
            logger.warning(f"Setting date to current time for file ID '{file_id}' "
                           f"(could not determine from frontmatter or Drive).")
            final_date_dt = datetime.now(self.tokyo_tz)
        post.metadata['date'] = final_date_dt # Store as datetime object for now

        # Determine 'lastmod' (similar logic to date, using modifiedTime as fallback)
        current_lastmod_val = post.get('lastmod')
        final_lastmod_dt: Optional[datetime] = None
        if isinstance(current_lastmod_val, datetime): # Handle existing datetime objects
            final_lastmod_dt = current_lastmod_val
            if final_lastmod_dt.tzinfo is None: # Ensure timezone awareness
                 logger.warning(f"Existing lastmod '{current_lastmod_val}' is naive. "
                                f"Assigning default timezone {DEFAULT_TIMEZONE}.")
                 final_lastmod_dt = final_lastmod_dt.replace(tzinfo=self.tokyo_tz)
            else: # Convert to target timezone if different
                 final_lastmod_dt = final_lastmod_dt.astimezone(self.tokyo_tz)
        elif isinstance(current_lastmod_val, date): # Handle date objects (from YYYY-MM-DD)
             logger.info(f"Frontmatter provided lastmod date object: {current_lastmod_val}. Converting to datetime.")
             # Combine date with midnight time and add default timezone
             final_lastmod_dt = datetime.combine(current_lastmod_val, datetime.min.time()).replace(tzinfo=self.tokyo_tz)
        elif isinstance(current_lastmod_val, str): # Handle string values
            try:
                # Use dateutil.parser.parse for flexible parsing after stripping whitespace
                lastmod_str_stripped = current_lastmod_val.strip()
                logger.info(f"Attempting to parse stripped lastmod string '{lastmod_str_stripped}' using dateutil.")
                dt_parsed = dateutil_parser.parse(lastmod_str_stripped)

                # Handle timezone: if naive, assume default; if aware, convert
                if dt_parsed.tzinfo is None or dt_parsed.tzinfo.utcoffset(dt_parsed) is None:
                    logger.warning(f"Parsed lastmod '{current_lastmod_val}' is naive. "
                                   f"Assigning default timezone {DEFAULT_TIMEZONE}.")
                    final_lastmod_dt = dt_parsed.replace(tzinfo=self.tokyo_tz)
                else:
                    logger.info(f"Parsed lastmod '{current_lastmod_val}' has timezone. Converting to {DEFAULT_TIMEZONE}.")
                    final_lastmod_dt = dt_parsed.astimezone(self.tokyo_tz)

            except (ValueError, OverflowError, TypeError) as parse_err:
                logger.warning(f"Could not parse existing lastmod string '{current_lastmod_val}' "
                               f"using dateutil: {parse_err}. Falling back to Drive modifiedTime.")
                final_lastmod_dt = self._parse_iso_datetime(drive_modified_str)
        else: # Not datetime or string, use Drive time
            final_lastmod_dt = self._parse_iso_datetime(drive_modified_str)

        # Fallback if lastmod is still None after all attempts
        if not isinstance(final_lastmod_dt, datetime):
            logger.warning(f"Setting lastmod to final date value for file ID '{file_id}' "
                           f"(could not determine from frontmatter or Drive).")
            # Use the determined date as fallback, ensuring it's a datetime
            if isinstance(final_date_dt, datetime):
                final_lastmod_dt = final_date_dt
            else: # Should not happen due to date fallback, but safety check
                logger.error(f"Cannot set lastmod fallback for file ID {file_id} as date is not valid.")
                final_lastmod_dt = datetime.now(self.tokyo_tz) # Ultimate fallback
        post.metadata['lastmod'] = final_lastmod_dt # Store as datetime object for now

        # Set other metadata
        if not post.get('title'): post.metadata['title'] = file_name
        if 'draft' not in post.metadata: post.metadata['draft'] = False
        post.metadata['google_drive_id'] = file_id
        logger.info(f"[{file_id}] process_content: Setting metadata 'modifiedTime' to Drive's value: '{drive_modified_str}'")
        post.metadata['modifiedTime'] = drive_modified_str
        post.metadata.pop('conversion_error', None) # Clear previous errors

        # Process images
        try:
            post.content = self._process_images(post.content)
        except Exception as img_err:
            logger.exception(f"Error during image processing for file ID '{file_id}'")
            post.metadata['conversion_error'] = f'Image processing error: {img_err}'

        # Replace escaped blockquotes at the beginning of lines
        try:
            logger.info(f"[{file_id}] Replacing escaped blockquotes '^\\> ' with '> ' at the beginning of lines")
            post.content = re.sub(r'^\\> ', '> ', post.content, flags=re.MULTILINE)
        except Exception as e:
            logger.exception(f"Error replacing escaped blockquotes for file ID '{file_id}': {e}")
            # Optionally, add a note to metadata if this specific step fails
            post.metadata['conversion_error'] = post.metadata.get('conversion_error', '') + f'; Blockquote unescaping error: {e}'

        # Format dates and dump
        try:
            post.metadata['date'] = self._format_datetime(post.metadata.get('date'))
            post.metadata['lastmod'] = self._format_datetime(post.metadata.get('lastmod'))
            post.metadata = {k: v for k, v in post.metadata.items() if v is not None}

            # Log the modifiedTime value just before dumping
            final_modified_time_in_metadata = post.metadata.get('modifiedTime')
            logger.info(f"[{file_id}] process_content: 'modifiedTime' in metadata before dump: '{final_modified_time_in_metadata}' (type: {type(final_modified_time_in_metadata)})")

            dumped_content = frontmatter.dumps(post)

            # Log the modifiedTime from the dumped string (for verification)
            try:
                # Quick check if the dumped string contains the expected modifiedTime
                # This is a simple check; a more robust check might involve parsing the dumped YAML
                if isinstance(final_modified_time_in_metadata, str):
                    expected_fm_line = f"modifiedTime: '{final_modified_time_in_metadata}'" # Note: python-frontmatter might not quote if not needed
                    expected_fm_line_alt = f"modifiedTime: {final_modified_time_in_metadata}"
                    if expected_fm_line in dumped_content or expected_fm_line_alt in dumped_content :
                        logger.info(f"[{file_id}] process_content: Verified 'modifiedTime' seems present as expected in dumped content.")
                    else:
                        # Extract the actual modifiedTime line from dumped content for logging
                        actual_mt_line = "Not found or complex structure"
                        for line in dumped_content.splitlines():
                            if line.startswith("modifiedTime:"):
                                actual_mt_line = line
                                break
                        logger.warning(f"[{file_id}] process_content: 'modifiedTime' in dumped content might differ or not be found as simple string. Actual line: '{actual_mt_line}'")
            except Exception as log_dump_err:
                logger.warning(f"[{file_id}] process_content: Could not verify 'modifiedTime' in dumped string due to: {log_dump_err}")

            return dumped_content
        except Exception as dump_error:
            logger.critical(
                f"[{file_id}] Failed to dump final frontmatter for file ID '{file_id}'. Error: {dump_error}"
            )
            error_content = (
                f"---\n"
                f"title: {post.metadata.get('title', 'ERROR')}\n"
                f"google_drive_id: {file_id}\n"
                f"modifiedTime: {drive_modified_str or 'ERROR'}\n"
                f"conversion_error: 'CRITICAL DUMP ERROR - {dump_error}'\n"
                f"---\n\n{post.content}"
            )
            return error_content

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
    """Wrapper function executed by each process in a ProcessPoolExecutor.

    Returns:
        Tuple[ProcessStatus, bool]: (status, is_draft)
                                    is_draft is True if the file is a draft,
                                    False otherwise. Defaults to True on errors.
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
            is_draft = False # If content was processed, assume not draft if unspecified

        if processor.save_markdown(file_id, processed_content_str):
            return 'success', is_draft
        else:
            return 'save_error', is_draft # Return determined draft status even on save error

    except Exception as e:
        logger.exception(f'Unexpected error during task execution for file ID {file_id}')
        return 'unknown_error', True # Default to draft on major error


# --- Main Execution ---

def main() -> None:
    """Main function to orchestrate the document conversion process."""
    start_time = time.time()
    logger.info(f'Starting Google Docs to Markdown conversion at {datetime.now()}...')

    parent_folder_id = os.getenv('GOOGLE_DRIVE_PARENT_ID')
    if not parent_folder_id:
        logger.critical('GOOGLE_DRIVE_PARENT_ID environment variable is not set.')
        exit(1)

    logger.info(f'Target Parent Folder ID: {parent_folder_id}')
    output_path = Path(OUTPUT_SUBDIR)
    logger.info(f'Output directory: {output_path.resolve()}')

    try:
        # Initialize client for listing
        initial_client = GoogleDriveClient()
    except RuntimeError as e:
        logger.critical(f'Failed to initialize Google Drive client: {e}')
        exit(1)

    drive_files: List[DriveMetadata] = initial_client.list_google_docs(parent_folder_id)
    current_drive_ids = {f['id'] for f in drive_files if f.get('id')}
    logger.info(f'Found {len(current_drive_ids)} unique file IDs in Google Drive.')

    # Sync local files
    logger.info(f'Checking local files in {output_path.resolve()} for cleanup...')
    deleted_public_files_count = 0

    if output_path.exists():
        local_md_files_paths = list(output_path.glob('*.md'))
        local_files_metadata: Dict[str, Dict[str, Any]] = {} # Store {'file_id': {'path': Path, 'is_draft': bool}}

        for local_path_obj in local_md_files_paths:
            file_id = local_path_obj.stem
            if re.match(r'^[a-zA-Z0-9_-]+$', file_id): # Basic check for Drive ID format
                is_draft = True # Default to draft if parsing fails
                try:
                    with open(local_path_obj, 'r', encoding='utf-8') as f:
                        post = frontmatter.load(f)
                        is_draft = post.get('draft', False)
                    local_files_metadata[file_id] = {'path': local_path_obj, 'is_draft': is_draft}
                except Exception as e:
                    logger.warning(f"Could not read/parse {local_path_obj} to get draft status during cleanup pre-check: {e}. Assuming draft.")
                    local_files_metadata[file_id] = {'path': local_path_obj, 'is_draft': True} # Assume draft
            else:
                logger.warning(f'Found local file with unexpected name format: {local_path_obj.name}. Skipping cleanup consideration.')

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
                    # Should not happen if logic is correct, but as a safeguard:
                    logger.warning(f"Attempted to delete file ID {file_id_to_delete} but its metadata was not pre-scanned. Skipping deletion.")
        else:
            logger.info('No local files need deletion.')
    else:
        logger.info('Output directory does not exist, skipping cleanup.')

    if not drive_files and deleted_public_files_count == 0: # Also check if a public file was deleted
        logger.info('No Google Docs found to process and no public files deleted.')
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
        exit_code = 1
    else:
        logger.info('Conversion completed successfully.')
        exit_code = 0

    # --- Marker File Logic ---
    public_content_processed_and_not_draft = results['public_updated'] > 0

    # Marker creation condition:
    # Deploy if any non-draft content was successfully updated/created OR if any public file was deleted.
    should_create_marker = public_content_processed_and_not_draft or (deleted_public_files_count > 0)

    marker_file = Path('.content-updated')
    logger.info(
        f"Marker logic check: public_content_processed_not_draft: {public_content_processed_and_not_draft}, "
        f"deleted_public_files_count: {deleted_public_files_count}, "
        f"should_create_marker: {should_create_marker}"
    )

    if should_create_marker:
        logger.info("Public content updated or public file deleted. Attempting to create marker file '.content-updated'...")
        try:
            marker_file.touch()
            if marker_file.exists():
                 logger.info("Successfully created marker file.")
            else:
                 logger.error("Marker file creation attempted but file does not exist afterwards.")
        except OSError as e:
            logger.error(f"Failed to create marker file: {e}")
    else:
        logger.info("No public content updates or public file deletions detected, marker file will not be created/will be removed.")
        if marker_file.exists():
             logger.info("Attempting to remove existing marker file as no relevant updates occurred...")
             try:
                 marker_file.unlink()
                 logger.info("Successfully removed existing marker file.")
             except OSError as e:
                 logger.warning(f"Could not remove existing marker file: {e}")

    logger.info(f"Exiting with code: {exit_code}")
    exit(exit_code)


if __name__ == '__main__':
    main()
