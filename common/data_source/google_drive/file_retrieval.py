import logging
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from enum import Enum

from googleapiclient.discovery import Resource  # type: ignore
from googleapiclient.errors import HttpError  # type: ignore

from common.data_source.google_drive.constant import DRIVE_FOLDER_TYPE, DRIVE_SHORTCUT_TYPE
from common.data_source.google_drive.model import DriveRetrievalStage, GoogleDriveFileType, RetrievedDriveFile
from common.data_source.google_util.resource import GoogleDriveService
from common.data_source.google_util.util import ORDER_BY_KEY, PAGE_TOKEN_KEY, GoogleFields, execute_paginated_retrieval, execute_paginated_retrieval_with_max_pages
from common.data_source.models import SecondsSinceUnixEpoch

PERMISSION_FULL_DESCRIPTION = "permissions(id, emailAddress, type, domain, permissionDetails)"

FILE_FIELDS = "nextPageToken, files(mimeType, id, name, modifiedTime, webViewLink, shortcutDetails, owners(emailAddress), size)"
FILE_FIELDS_WITH_PERMISSIONS = f"nextPageToken, files(mimeType, id, name, {PERMISSION_FULL_DESCRIPTION}, permissionIds, modifiedTime, webViewLink, shortcutDetails, owners(emailAddress), size)"
SLIM_FILE_FIELDS = f"nextPageToken, files(mimeType, driveId, id, name, {PERMISSION_FULL_DESCRIPTION}, permissionIds, webViewLink, owners(emailAddress), modifiedTime)"

FOLDER_FIELDS = "nextPageToken, files(id, name, permissions, modifiedTime, webViewLink, shortcutDetails)"


class DriveFileFieldType(Enum):
    """Enum to specify which fields to retrieve from Google Drive files"""

    DELETION = "deletion"  # Identity-only, fail-closed inventory
    SLIM = "slim"  # Minimal fields for basic file info
    STANDARD = "standard"  # Standard fields including content metadata
    WITH_PERMISSIONS = "with_permissions"  # Full fields including permissions


def generate_time_range_filter(
    start: SecondsSinceUnixEpoch | None = None,
    end: SecondsSinceUnixEpoch | None = None,
) -> str:
    """Build a Google Drive API query filter clause for the given time range.

    Checks both modifiedTime and createdTime so that files uploaded with
    older modification timestamps are still discovered on incremental syncs.
    """
    time_range_filter = ""
    if start is not None:
        time_start = datetime.fromtimestamp(start, tz=UTC).isoformat()
        time_range_filter += f" and ({GoogleFields.MODIFIED_TIME.value} > '{time_start}' or {GoogleFields.CREATED_TIME.value} >= '{time_start}')"
    if end is not None:
        time_stop = datetime.fromtimestamp(end, tz=UTC).isoformat()
        time_range_filter += f" and {GoogleFields.MODIFIED_TIME.value} <= '{time_stop}'"
    return time_range_filter


def _get_folders_in_parent(
    service: Resource,
    parent_id: str | None = None,
    require_complete: bool = False,
) -> Iterator[GoogleDriveFileType]:
    # Follow shortcuts to folders
    query = f"(mimeType = '{DRIVE_FOLDER_TYPE}' or mimeType = '{DRIVE_SHORTCUT_TYPE}')"
    query += " and trashed = false"

    if parent_id:
        query += f" and '{parent_id}' in parents"

    yield from execute_paginated_retrieval(
        retrieval_function=service.files().list,
        list_key="files",
        continue_on_404_or_403=not require_complete,
        corpora="allDrives",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
        fields="kind,nextPageToken,incompleteSearch,files(id,name,mimeType,shortcutDetails)" if require_complete else FOLDER_FIELDS,
        require_complete=require_complete,
        q=query,
    )


def _get_fields_for_file_type(field_type: DriveFileFieldType) -> str:
    """Get the appropriate fields string based on the field type enum"""
    if field_type == DriveFileFieldType.DELETION:
        return "kind,nextPageToken,incompleteSearch,files(id,mimeType,name,webViewLink)"
    if field_type == DriveFileFieldType.SLIM:
        return SLIM_FILE_FIELDS
    elif field_type == DriveFileFieldType.WITH_PERMISSIONS:
        return FILE_FIELDS_WITH_PERMISSIONS
    else:  # DriveFileFieldType.STANDARD
        return FILE_FIELDS


def _get_files_in_parent(
    service: Resource,
    parent_id: str,
    field_type: DriveFileFieldType,
    start: SecondsSinceUnixEpoch | None = None,
    end: SecondsSinceUnixEpoch | None = None,
) -> Iterator[GoogleDriveFileType]:
    query = f"mimeType != '{DRIVE_FOLDER_TYPE}' and '{parent_id}' in parents"
    query += " and trashed = false"
    if field_type != DriveFileFieldType.DELETION:
        query += generate_time_range_filter(start, end)

    kwargs = {ORDER_BY_KEY: GoogleFields.MODIFIED_TIME.value}

    yield from execute_paginated_retrieval(
        retrieval_function=service.files().list,
        list_key="files",
        continue_on_404_or_403=field_type != DriveFileFieldType.DELETION,
        require_complete=field_type == DriveFileFieldType.DELETION,
        corpora="allDrives",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
        fields=_get_fields_for_file_type(field_type),
        q=query,
        **kwargs,
    )


def crawl_folders_for_files(
    service: Resource,
    parent_id: str,
    field_type: DriveFileFieldType,
    user_email: str,
    traversed_parent_ids: set[str],
    update_traversed_ids_func: Callable[[str], None],
    start: SecondsSinceUnixEpoch | None = None,
    end: SecondsSinceUnixEpoch | None = None,
) -> Iterator[RetrievedDriveFile]:
    """
    This function starts crawling from any folder. It is slower though.
    """
    require_complete = field_type == DriveFileFieldType.DELETION
    if require_complete:
        if parent_id in traversed_parent_ids:
            return
        parent = service.files().get(fileId=parent_id, fields="id,mimeType", supportsAllDrives=True).execute()
        if parent.get("id") != parent_id or parent.get("mimeType") != DRIVE_FOLDER_TYPE:
            raise ValueError("Google Drive deletion scope is not an accessible folder")
    logging.info("Entered crawl_folders_for_files with parent_id: " + parent_id)
    if parent_id not in traversed_parent_ids:
        logging.info("Parent id not in traversed parent ids, getting files")
        found_files = False
        file = {}
        try:
            for file in _get_files_in_parent(
                service=service,
                parent_id=parent_id,
                field_type=field_type,
                start=start,
                end=end,
            ):
                logging.info(f"Found file: {file['name']}, user email: {user_email}")
                found_files = True
                yield RetrievedDriveFile(
                    drive_file=file,
                    user_email=user_email,
                    parent_id=parent_id,
                    completion_stage=DriveRetrievalStage.FOLDER_FILES,
                )
            # Only mark a folder as done if it was fully traversed without errors
            # This usually indicates that the owner of the folder was impersonated.
            # In cases where this never happens, most likely the folder owner is
            # not part of the Google Workspace in question (or for oauth, the authenticated
            # user doesn't own the folder)
            if found_files or require_complete:
                update_traversed_ids_func(parent_id)
        except Exception as e:
            if require_complete:
                raise
            if isinstance(e, HttpError) and e.status_code == 403:
                # don't yield an error here because this is expected behavior
                # when a user doesn't have access to a folder
                logging.debug(f"Error getting files in parent {parent_id}: {e}")
            else:
                logging.error(f"Error getting files in parent {parent_id}: {e}")
                yield RetrievedDriveFile(
                    drive_file=file,
                    user_email=user_email,
                    parent_id=parent_id,
                    completion_stage=DriveRetrievalStage.FOLDER_FILES,
                    error=e,
                )
    else:
        logging.info(f"Skipping subfolder files since already traversed: {parent_id}")

    for subfolder in _get_folders_in_parent(
        service=service,
        parent_id=parent_id,
        require_complete=require_complete,
    ):
        if require_complete and subfolder.get("mimeType") == DRIVE_SHORTCUT_TYPE:
            details = subfolder.get("shortcutDetails")
            if not isinstance(details, dict) or any(not isinstance(details.get(key), str) or not details[key].strip() for key in ("targetId", "targetMimeType")):
                raise ValueError("Incomplete Google Drive folder shortcut")
            if details["targetMimeType"] != DRIVE_FOLDER_TYPE:
                continue
            subfolder = {**subfolder, "id": details["targetId"]}
        logging.info("Fetching all files in subfolder: " + subfolder["name"])
        yield from crawl_folders_for_files(
            service=service,
            parent_id=subfolder["id"],
            field_type=field_type,
            user_email=user_email,
            traversed_parent_ids=traversed_parent_ids,
            update_traversed_ids_func=update_traversed_ids_func,
            start=start,
            end=end,
        )


def get_files_in_shared_drive(
    service: Resource,
    drive_id: str,
    field_type: DriveFileFieldType,
    max_num_pages: int,
    update_traversed_ids_func: Callable[[str], None] = lambda _: None,
    cache_folders: bool = True,
    start: SecondsSinceUnixEpoch | None = None,
    end: SecondsSinceUnixEpoch | None = None,
    page_token: str | None = None,
) -> Iterator[GoogleDriveFileType | str]:
    kwargs = {ORDER_BY_KEY: GoogleFields.MODIFIED_TIME.value}
    if page_token:
        logging.info(f"Using page token: {page_token}")
        kwargs[PAGE_TOKEN_KEY] = page_token

    if cache_folders and field_type != DriveFileFieldType.DELETION:
        # If we know we are going to folder crawl later, we can cache the folders here
        # Get all folders being queried and add them to the traversed set
        folder_query = f"mimeType = '{DRIVE_FOLDER_TYPE}'"
        folder_query += " and trashed = false"
        for folder in execute_paginated_retrieval(
            retrieval_function=service.files().list,
            list_key="files",
            continue_on_404_or_403=field_type != DriveFileFieldType.DELETION,
            require_complete=field_type == DriveFileFieldType.DELETION,
            corpora="drive",
            driveId=drive_id,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            fields="nextPageToken, files(id)",
            q=folder_query,
        ):
            update_traversed_ids_func(folder["id"])

    # Get all files in the shared drive
    file_query = f"mimeType != '{DRIVE_FOLDER_TYPE}'"
    file_query += " and trashed = false"
    if field_type != DriveFileFieldType.DELETION:
        file_query += generate_time_range_filter(start, end)

    for file in execute_paginated_retrieval_with_max_pages(
        retrieval_function=service.files().list,
        max_num_pages=max_num_pages,
        list_key="files",
        continue_on_404_or_403=field_type != DriveFileFieldType.DELETION,
        require_complete=field_type == DriveFileFieldType.DELETION,
        corpora="drive",
        driveId=drive_id,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
        fields=_get_fields_for_file_type(field_type),
        q=file_query,
        **kwargs,
    ):
        # If we found any files, mark this drive as traversed. When a user has access to a drive,
        # they have access to all the files in the drive. Also, not a huge deal if we re-traverse
        # empty drives.
        # NOTE: ^^ the above is not actually true due to folder restrictions:
        # https://support.google.com/a/users/answer/12380484?hl=en
        # So we may have to change this logic for people who use folder restrictions.
        if field_type != DriveFileFieldType.DELETION:
            update_traversed_ids_func(drive_id)
        yield file
        if isinstance(file, str):
            return
    update_traversed_ids_func(drive_id)


def get_all_files_in_my_drive_and_shared(
    service: GoogleDriveService,
    update_traversed_ids_func: Callable,
    field_type: DriveFileFieldType,
    include_shared_with_me: bool,
    max_num_pages: int,
    start: SecondsSinceUnixEpoch | None = None,
    end: SecondsSinceUnixEpoch | None = None,
    cache_folders: bool = True,
    page_token: str | None = None,
) -> Iterator[GoogleDriveFileType | str]:
    kwargs = {ORDER_BY_KEY: GoogleFields.MODIFIED_TIME.value}
    if page_token:
        logging.info(f"Using page token: {page_token}")
        kwargs[PAGE_TOKEN_KEY] = page_token

    if cache_folders and field_type != DriveFileFieldType.DELETION:
        # If we know we are going to folder crawl later, we can cache the folders here
        # Get all folders being queried and add them to the traversed set
        folder_query = f"mimeType = '{DRIVE_FOLDER_TYPE}'"
        folder_query += " and trashed = false"
        if not include_shared_with_me:
            folder_query += " and 'me' in owners"
        found_folders = False
        for folder in execute_paginated_retrieval(
            retrieval_function=service.files().list,
            list_key="files",
            corpora="user",
            fields=_get_fields_for_file_type(field_type),
            q=folder_query,
        ):
            update_traversed_ids_func(folder[GoogleFields.ID])
            found_folders = True
        if found_folders:
            update_traversed_ids_func(get_root_folder_id(service))

    # Then get the files
    file_query = f"mimeType != '{DRIVE_FOLDER_TYPE}'"
    file_query += " and trashed = false"
    if not include_shared_with_me:
        file_query += " and 'me' in owners"
    if field_type != DriveFileFieldType.DELETION:
        file_query += generate_time_range_filter(start, end)
    yield from execute_paginated_retrieval_with_max_pages(
        retrieval_function=service.files().list,
        max_num_pages=max_num_pages,
        list_key="files",
        continue_on_404_or_403=False,
        require_complete=field_type == DriveFileFieldType.DELETION,
        corpora="user",
        fields=_get_fields_for_file_type(field_type),
        q=file_query,
        **kwargs,
    )


def get_all_files_for_oauth(
    service: GoogleDriveService,
    include_files_shared_with_me: bool,
    include_my_drives: bool,
    # One of the above 2 should be true
    include_shared_drives: bool,
    field_type: DriveFileFieldType,
    max_num_pages: int,
    start: SecondsSinceUnixEpoch | None = None,
    end: SecondsSinceUnixEpoch | None = None,
    page_token: str | None = None,
) -> Iterator[GoogleDriveFileType | str]:
    kwargs = {ORDER_BY_KEY: GoogleFields.MODIFIED_TIME.value}
    if page_token:
        logging.info(f"Using page token: {page_token}")
        kwargs[PAGE_TOKEN_KEY] = page_token

    should_get_all = include_shared_drives and include_my_drives and include_files_shared_with_me
    corpora = "allDrives" if should_get_all else "user"

    file_query = f"mimeType != '{DRIVE_FOLDER_TYPE}'"
    file_query += " and trashed = false"
    if field_type != DriveFileFieldType.DELETION:
        file_query += generate_time_range_filter(start, end)

    if not should_get_all:
        if include_files_shared_with_me and not include_my_drives:
            file_query += " and not 'me' in owners"
        if not include_files_shared_with_me and include_my_drives:
            file_query += " and 'me' in owners"

    yield from execute_paginated_retrieval_with_max_pages(
        max_num_pages=max_num_pages,
        retrieval_function=service.files().list,
        list_key="files",
        continue_on_404_or_403=False,
        require_complete=field_type == DriveFileFieldType.DELETION,
        corpora=corpora,
        includeItemsFromAllDrives=should_get_all,
        supportsAllDrives=should_get_all,
        fields=_get_fields_for_file_type(field_type),
        q=file_query,
        **kwargs,
    )


# Just in case we need to get the root folder id
def get_root_folder_id(service: Resource) -> str:
    # we don't paginate here because there is only one root folder per user
    # https://developers.google.com/drive/api/guides/v2-to-v3-reference
    return service.files().get(fileId="root", fields=GoogleFields.ID.value).execute()[GoogleFields.ID.value]
