"""PythonAnywhere WSGI file — for hmmanya.pythonanywhere.com

WHAT TO DO WITH THIS FILE
-------------------------
Do NOT rely on this copy being imported. PythonAnywhere only ever loads the file
linked from the *Web* tab, which for this account is:

    /var/www/hmmanya_pythonanywhere_com_wsgi.py

Open that link on the Web tab, delete everything already in it, paste the
contents of this file, save, and hit Reload.

CHECK THE PATH BELOW
--------------------
PROJECT_DIR must be the folder that directly contains manage.py. Find it with:

    ls ~/hair-saloon/manage.py            # if this works, keep the path as-is
    ls ~/hair-saloon/hair-saloon/manage.py  # if THIS is the one that works,
                                            # use the nested path instead

Both are plausible depending on how the repository was cloned, which is why the
code below checks and raises a clear error rather than failing with a confusing
"No module named hairsaloon".
"""
import os
import sys

USERNAME = "hmmanya"

# The folder containing manage.py. Change this if the check below complains.
PROJECT_DIR = f"/home/{USERNAME}/hair-saloon"

# Fall back to the nested layout automatically if that is where manage.py lives.
if not os.path.exists(os.path.join(PROJECT_DIR, "manage.py")):
    nested = os.path.join(PROJECT_DIR, "hair-saloon")
    if os.path.exists(os.path.join(nested, "manage.py")):
        PROJECT_DIR = nested
    else:
        raise RuntimeError(
            f"manage.py not found in {PROJECT_DIR} or {nested}. "
            "Set PROJECT_DIR in this file to the folder that contains manage.py."
        )

if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

os.environ["DJANGO_SETTINGS_MODULE"] = "hairsaloon.settings"

# settings.py reads PROJECT_DIR/.env by itself, so there is nothing else to set
# here. Keep secrets in .env, never in this file — it lives outside the project
# and is easy to forget about.
from django.core.wsgi import get_wsgi_application  # noqa: E402

application = get_wsgi_application()
