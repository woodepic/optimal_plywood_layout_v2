"""Design-side advice: small dimensional changes that pay for themselves."""
import argparse

from src.advise import advise
from src.model import CutConfig
from src.parts import load_demand

ap = argparse.ArgumentParser()
ap.add_argument("step", nargs="?", default="Master Kitchen Layout V3.step")
a = ap.parse_args()

cfg = CutConfig()
print(advise(load_demand(a.step, cfg), cfg))
