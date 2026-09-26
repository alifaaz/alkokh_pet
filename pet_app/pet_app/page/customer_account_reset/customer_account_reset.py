from pet_app.api.accounting.account_reset import require_admin


def get_context(context):
    require_admin()
