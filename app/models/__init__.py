# Importer ici chaque modèle pour qu'Alembic les découvre via app.db.base.Base.
from app.models.energy import IngestionEtat, Mesure, MesureExclu, Site
from app.models.observation import Alerte, CapteurEtat, CapteurPanne
from app.models.prediction import Modele, Prediction
from app.models.simulation import SimulationPic
from app.models.user import AppUser

__all__ = [
    "Alerte",
    "AppUser",
    "CapteurEtat",
    "CapteurPanne",
    "IngestionEtat",
    "Mesure",
    "MesureExclu",
    "Modele",
    "Prediction",
    "SimulationPic",
    "Site",
]
