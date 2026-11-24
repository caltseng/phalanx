"""Server-owned direct dispatch settings."""


def set_dispatch(db, settings):
    db.execute("UPDATE dispatch_settings SET paused=?, aging_seconds=? WHERE id=1",
               (settings.paused, settings.aging_seconds))
    return settings.model_dump()
