# Importer ici chaque modèle pour qu'Alembic les découvre via app.db.base.Base.
from app.models.energy import Mesure, Site
from app.models.user import AppUser

__all__ = ["AppUser", "Mesure", "Site"]
