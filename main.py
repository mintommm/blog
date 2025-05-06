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
                    item_name = item.get('name', f"Unnamed Item (ID: {item_id})")

                    if mime_type == MIME_TYPE_FOLDER:
                        logger.info(f'Scanning subfolder: {item_name} ({item_id})')
                        if item_id:
                            try:
                                # Recursive call uses the same client instance
                                all_files.extend(self.list_google_docs(item_id))
                            except Exception as sub_error:
                                logger.error(
                                    f'Error scanning subfolder {item_name}: '
                                    f'{sub_error}. Skipping folder.'
                                )
                        else:
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

    def download_markdown(self, file_id: str, file_name: str) -> Optional[str]:
        """Downloads a Google Doc as Markdown, handling retries."""
        # Uses self.service which is built with requestBuilder
        logger.info(f'Attempting download: {file_name} ({file_id})')
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
            logger.info(f'Successfully downloaded: {file_name}')
            return downloaded_fh.getvalue().decode('utf-8')

        except (HttpError, Exception) as error:
            logger.error(
                f'Failed to download {file_name} (ID: {file_id}) after '
                f'retries: {error}'
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
    ) -> bool:
        """Checks if the local file cache is up-to-date."""
        local_path = self.get_local_path(file_id)
        if not drive_modified_time_str:
            logger.warning(f'Drive modifiedTime missing for {file_id}. Forcing update.')
            return False

        if local_path.exists():
            try:
                with open(local_path, 'r', encoding='utf-8') as f:
                    local_post = frontmatter.load(f)
                local_modified_time_str = local_post.get('modifiedTime')

                if (local_modified_time_str and
                        local_modified_time_str == drive_modified_time_str):
                    logger.info(f"Skipping '{file_id}': Local 'modifiedTime' matches Drive's.")
                    return True
                elif not local_modified_time_str:
                    logger.warning(
                        f"Local 'modifiedTime' not found in {local_path}. Forcing update."
                    )
            except Exception as e:
                logger.warning(
                    f'Error reading or parsing local file {local_path}: {e}. '
                    f'Forcing update.'
                )
        return False

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
        file_name = drive_metadata.get('name', 'Unknown File')
        file_id = drive_metadata.get('id', 'Unknown ID')

        try:
            post = frontmatter.loads(md_content)
        except Exception as parse_error:
            logger.critical(
                f"Failed to parse frontmatter for '{file_name}' ({file_id}). "
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
            logger.warning(f"Setting date to current time for '{file_name}' "
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
            logger.warning(f"Setting lastmod to final date value for '{file_name}' "
                           f"(could not determine from frontmatter or Drive).")
            # Use the determined date as fallback, ensuring it's a datetime
            if isinstance(final_date_dt, datetime):
                final_lastmod_dt = final_date_dt
            else: # Should not happen due to date fallback, but safety check
                logger.error(f"Cannot set lastmod fallback for {file_name} as date is not valid.")
                final_lastmod_dt = datetime.now(self.tokyo_tz) # Ultimate fallback
        post.metadata['lastmod'] = final_lastmod_dt # Store as datetime object for now

        # Set other metadata
        if not post.get('title'): post.metadata['title'] = file_name
        if 'draft' not in post.metadata: post.metadata['draft'] = False
        post.metadata['google_drive_id'] = file_id
        post.metadata['modifiedTime'] = drive_modified_str
        post.metadata.pop('conversion_error', None) # Clear previous errors

        # Process images
        try:
            post.content = self._process_images(post.content)
        except Exception as img_err:
            logger.exception(f"Error during image processing for '{file_name}'")
            post.metadata['conversion_error'] = f'Image processing error: {img_err}'

        # Format dates and dump
        try:
            post.metadata['date'] = self._format_datetime(post.metadata.get('date'))
            post.metadata['lastmod'] = self._format_datetime(post.metadata.get('lastmod'))
            post.metadata = {k: v for k, v in post.metadata.items() if v is not None}
            return frontmatter.dumps(post)
        except Exception as dump_error:
            logger.critical(
                f"Failed to dump final frontmatter for '{file_name}'. Error: {dump_error}"
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
) -> ProcessStatus:
    """Wrapper function executed by each process in a ProcessPoolExecutor."""
    file_id = drive_metadata.get('id')
    file_name = drive_metadata.get('name', f"ID: {file_id or 'Unknown'}")
    drive_modified_time = drive_metadata.get('modifiedTime')

    if not file_id:
        logger.error("File metadata missing 'id'. Cannot process.")
        return 'process_error'

    logger.info(f'Starting processing for: {file_name} ({file_id})')

    # Each process needs its own client and processor instances
    try:
        processor = MarkdownProcessor(output_dir_str)
        # Create a new client instance in the subprocess. This will build
        # its own service object using the requestBuilder, ensuring safety.
        client = GoogleDriveClient()
    except Exception as init_error:
        logger.exception(f'Error initializing client/processor for {file_name}')
        return 'init_error'

    try:
        if processor.check_cache(file_id, drive_modified_time):
            return 'skipped'

        # Use the client instance created within this process
        md_content = client.download_markdown(file_id, file_name)
        if md_content is None: return 'download_error'

        processed_content = processor.process_content(md_content, drive_metadata)
        if processed_content is None: return 'process_error'

        if processor.save_markdown(file_id, processed_content):
            return 'success'
        else:
            return 'save_error'

    except Exception as e:
        logger.exception(f'Unexpected error during task execution for {file_name}')
        return 'unknown_error'


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
    if output_path.exists():
        local_md_files = list(output_path.glob('*.md'))
        local_ids = set()
        for local_file in local_md_files:
            file_id = local_file.stem
            if re.match(r'^[a-zA-Z0-9_-]+$', file_id): # Basic check for Drive ID format
                local_ids.add(file_id)
            else:
                logger.warning(f'Found local file with unexpected name format: {local_file.name}. Skipping.')

        ids_to_delete = local_ids - current_drive_ids
        if ids_to_delete:
            logger.info(f'Found {len(ids_to_delete)} local files to delete:')
            for file_id_to_delete in ids_to_delete:
                local_file_path = output_path / f'{file_id_to_delete}.md'
                logger.info(f'  - Deleting {local_file_path.name}')
                try:
                    local_file_path.unlink()
                except OSError as e:
                    logger.error(f'    Error deleting file {local_file_path}: {e}')
        else:
            logger.info('No local files need deletion.')
    else:
        logger.info('Output directory does not exist, skipping cleanup.')

    if not drive_files:
        logger.info('No Google Docs found to process after cleanup check.')
        logger.info('Conversion finished.')
        return

    logger.info(f'Submitting {len(drive_files)} Google Docs for processing...')

    # Parallel Processing
    results: Dict[str, int] = {'success': 0, 'skipped': 0, 'failed': 0}
    output_dir_abs_str = str(output_path.resolve())

    with concurrent.futures.ProcessPoolExecutor() as executor:
        future_to_file: Dict[concurrent.futures.Future, DriveMetadata] = {
            executor.submit(process_single_file_task, meta, output_dir_abs_str): meta
            for meta in drive_files
        }

        for future in concurrent.futures.as_completed(future_to_file):
            file_meta = future_to_file[future]
            file_id = file_meta.get('id', 'Unknown ID')
            file_name = file_meta.get('name', f'ID: {file_id}')
            try:
                status: ProcessStatus = future.result()
                if status == 'success': results['success'] += 1
                elif status == 'skipped': results['skipped'] += 1
                else:
                    results['failed'] += 1
                    logger.error(
                        f"Processing failed for '{file_name}' ({file_id}) "
                        f'with status: {status}'
                    )
            except Exception as exc:
                results['failed'] += 1
                logger.exception(
                    f"'{file_name}' ({file_id}) generated an unexpected "
                    f'exception during execution'
                )

    # Report Summary
    end_time = time.time()
    duration = end_time - start_time
    summary = (
        f"\n{'-'*30}\n"
        f"Conversion Summary:\n"
        f"  Duration: {duration:.2f} seconds\n"
        f"  Total Files Found: {len(drive_files)}\n"
        f"  Successfully Processed/Updated: {results['success']}\n"
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
    content_updated = results['success'] > 0 or ids_to_delete # Determine if content was actually changed
    marker_file = Path('.content-updated')
    logger.info(f"Marker logic check: content_updated flag is: {content_updated}")

    if content_updated:
        logger.info("Attempting to create marker file '.content-updated'...")
        try:
            marker_file.touch()
            if marker_file.exists():
                 logger.info("Successfully created marker file.")
            else:
                 logger.error("Marker file creation attempted but file does not exist afterwards.")
        except OSError as e:
            logger.error(f"Failed to create marker file: {e}")
            # Decide if this should cause a failure? For now, just log.
    else:
        logger.info("No content updates detected, marker file will not be created.")
        # Ensure marker file doesn't exist from previous runs if no updates
        if marker_file.exists():
             logger.info("Attempting to remove existing marker file as no updates occurred...")
             try:
                 marker_file.unlink()
                 logger.info("Successfully removed existing marker file.")
             except OSError as e:
                 logger.warning(f"Could not remove existing marker file: {e}")

    logger.info(f"Exiting with code: {exit_code}")
    exit(exit_code)


if __name__ == '__main__':
    main()
