import frappe

from ginesys_migration.ginesys_migration.doctype.ginesys_settings.ginesys_settings import (
    SETTINGS_DOCTYPE,
    update_sync_schedule,
)


def execute():
    """
    Create the scheduled sync's Scheduled Job Type from the saved Sync Time.

    It used to be a job declared in hooks.py that checked every minute; that one is
    removed by `bench migrate` now that it is no longer declared.
    """

    update_sync_schedule(frappe.get_single(SETTINGS_DOCTYPE))
