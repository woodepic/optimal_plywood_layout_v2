"""Design-side advice: small dimensional changes that pay for themselves."""
import argparse

from src.advise import advise
from src.model import CutConfig, load_config
from src.parts import load_demand

ap = argparse.ArgumentParser()
ap.add_argument("step", nargs="?", default="Master Kitchen Layout V3.step")
ap.add_argument("--config", default="config.json")
a = ap.parse_args()

import os
cfg = load_config(a.config) if a.config and os.path.exists(a.config) else CutConfig()
print(advise(load_demand(a.step, cfg), cfg))
