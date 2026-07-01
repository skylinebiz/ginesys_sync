from ginesys_migration.setup.custom_field import create_custom_fields


def after_install():
    create_custom_fields()