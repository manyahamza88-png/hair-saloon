from django.db.utils import DatabaseError, OperationalError, ProgrammingError

from .models import SalonSettings


def salon(request):
    """Make the salon settings available in every template."""
    try:
        settings_row = SalonSettings.load()
    except (OperationalError, ProgrammingError, DatabaseError):
        # Before the first migrate there is no table yet.
        settings_row = SalonSettings()
    return {"salon": settings_row}
