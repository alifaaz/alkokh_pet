"""Installed-app wrapper for the Alkokh French PCG setup module.

This bench has the package installed as pet_app, so this wrapper can be used
with:
	bench --site <site> execute pet_app.setup.french_pcg_vet_store.setup_french_pcg_vet_store --kwargs "{'company':'Kokh-vet'}"
"""

from alkokh.setup.french_pcg_vet_store import (  # noqa: F401
	setup_french_pcg_vet_store,
	smoke_check_french_pcg_vet_store,
)
