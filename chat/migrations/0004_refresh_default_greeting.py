"""The staff greeting used to carry the whole opening line.

Now that a chat opens with an automatic "Hi {name}, how may I help you?", the
old handover text repeats the question. Update installs that never customised
it; leave anybody's own wording alone.
"""
from django.db import migrations

OLD_DEFAULT = "Hi! A stylist has joined the chat. How can we help?"
NEW_DEFAULT = "{staff} here — I am with you now."


def refresh(apps, schema_editor):
    ChatSettings = apps.get_model("chat", "ChatSettings")
    ChatSettings.objects.filter(greeting=OLD_DEFAULT).update(greeting=NEW_DEFAULT)


def unrefresh(apps, schema_editor):
    ChatSettings = apps.get_model("chat", "ChatSettings")
    ChatSettings.objects.filter(greeting=NEW_DEFAULT).update(greeting=OLD_DEFAULT)


class Migration(migrations.Migration):
    dependencies = [("chat", "0003_chatsettings_auto_greeting_and_more")]
    operations = [migrations.RunPython(refresh, unrefresh)]
