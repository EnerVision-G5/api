# Importer ici chaque modèle pour qu'Alembic les découvre via app.db.base.Base.
from app.models.energy import IngestionEtat, Mesure, Site
from app.models.prediction import Modele, Prediction
from app.models.user import AppUser

__all__ = [
    "AppUser",
    "IngestionEtat",
    "Mesure",
    "Modele",
    "Prediction",
    "Site",
]
