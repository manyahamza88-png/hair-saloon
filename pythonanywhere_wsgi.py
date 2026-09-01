"""PythonAnywhere WSGI file.

Copy the contents of this file into the WSGI configuration file that the
PythonAnywhere "Web" tab links to (something like
``/var/www/yourname_pythonanywhere_com_wsgi.py``), then change USERNAME and
PROJECT below. Delete everything else that file came with.
"""
import os
import sys

# --- edit these two -------------------------------------------------------
USERNAME = "yourname"
PROJECT = "hair-saloon"
# -------------------------------------------------------------------------

path = f"/home/{USERNAME}/{PROJECT}"
if path not in sys.path:
    sys.path.insert(0, path)

os.environ["DJANGO_SETTINGS_MODULE"] = "hairsaloon.settings"

# settings.py reads /home/USERNAME/PROJECT/.env, so nothing else is needed here.
from django.core.wsgi import get_wsgi_application  # noqa: E402

application = get_wsgi_application()
