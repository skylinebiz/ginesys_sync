from ginesys_migration.setup.custom_fields import create_custom_fields


def after_install():
    create_custom_fields()