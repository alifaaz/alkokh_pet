"""Guards pet_app.install.INSTALL_SCHEMA_PATCHES against silent drift.

Why this test exists
--------------------
frappe.installer.install_app() calls set_all_patches_as_completed(), which writes a
Patch Log row for every entry in patches.txt *without executing it*. A later
`bench migrate` then skips them too, because they are already logged. So any patch
that builds schema - creates a DocType, or adds fields to one - never runs on a
fresh site unless install.py re-runs it explicitly.

That is what INSTALL_SCHEMA_PATCHES is for, and it is hand-maintained. It fell three
patches behind once already (p1_6/p1_7/p1_8 were added to patches.txt and never here),
which left six WhatsApp/push doctypes absent from every fresh install while the site
that authored them looked fine. Nothing failed loudly; the gap only showed up on a
rebuild attempt.

This test makes that failure loud and early. It is pure static analysis - no database,
no fixtures - so it runs anywhere the test suite runs and names the exact patch to add.

If this test fails, add the patch it names to INSTALL_SCHEMA_PATCHES in patches.txt
order. Do not add it to the exemption list unless the patch genuinely only mutates
existing rows on an already-populated site.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

from frappe.tests import UnitTestCase

from pet_app.install import INSTALL_SCHEMA_PATCHES


PATCHES_DIR = Path(__file__).resolve().parent.parent / "patches"
PATCHES_TXT = Path(__file__).resolve().parent.parent / "patches.txt"
MODULE_PREFIX = "pet_app.patches."

# Patches that touch DocType definitions only to clean up after an older release -
# renaming or deleting something that never exists on a fresh site. Running these at
# install time would be a no-op at best. Keep this list short and justified.
INSTALL_EXEMPT: frozenset[str] = frozenset()


def _patches_txt_order() -> list[str]:
	"""pet_app patch modules in patches.txt order, comments and sections stripped."""
	lines = PATCHES_TXT.read_text(encoding="utf-8").splitlines()
	return [
		line.strip()
		for line in lines
		if line.strip().startswith(MODULE_PREFIX) and not line.strip().startswith("#")
	]


def _doctypes_touched(module_path: Path) -> set[str]:
	"""DocType names a patch creates or extends, found without importing it.

	Recognises the three shapes used in this app:
	  * a module-level ``DOCTYPES = {"Name": {...}}`` spec dict
	  * a direct ``ensure_doctype("Name", {...})`` call
	  * a literal ``frappe.get_doc({"doctype": "DocType", "name": "Name"})``
	"""
	tree = ast.parse(module_path.read_text(encoding="utf-8"))
	found: set[str] = set()

	for node in ast.walk(tree):
		if isinstance(node, ast.Assign):
			for target in node.targets:
				if (
					isinstance(target, ast.Name)
					and target.id == "DOCTYPES"
					and isinstance(node.value, ast.Dict)
				):
					found |= {
						key.value
						for key in node.value.keys
						if isinstance(key, ast.Constant) and isinstance(key.value, str)
					}

		if (
			isinstance(node, ast.Call)
			and isinstance(node.func, ast.Name)
			and node.func.id == "ensure_doctype"
			and node.args
			and isinstance(node.args[0], ast.Constant)
			and isinstance(node.args[0].value, str)
		):
			found.add(node.args[0].value)

		if isinstance(node, ast.Dict):
			pairs = {
				key.value: value
				for key, value in zip(node.keys, node.values)
				if isinstance(key, ast.Constant) and isinstance(key.value, str)
			}
			doctype = pairs.get("doctype")
			if isinstance(doctype, ast.Constant) and doctype.value == "DocType":
				name = pairs.get("name")
				found.add(name.value if isinstance(name, ast.Constant) else "<dynamic>")

	return found


class TestInstallSchemaPatches(UnitTestCase):
	def test_every_schema_patch_runs_at_install(self):
		"""A patch that builds schema must be re-run by after_install()."""
		installed = set(INSTALL_SCHEMA_PATCHES)
		missing = []

		for module in _patches_txt_order():
			if module in INSTALL_EXEMPT:
				continue
			path = PATCHES_DIR / (module.rsplit(".", 1)[-1] + ".py")
			if not path.exists():
				continue
			doctypes = _doctypes_touched(path)
			if doctypes and f"{module}.execute" not in installed:
				missing.append((module, sorted(doctypes)))

		if missing:
			detail = "\n".join(
				f"  {module}.execute   builds: {', '.join(doctypes)}"
				for module, doctypes in missing
			)
			self.fail(
				"These patches build DocType schema but are not re-run on a fresh "
				"install, so their doctypes will not exist there:\n"
				f"{detail}\n\n"
				"install_app() marks patches.txt as applied without running it. Add "
				"each entry above to INSTALL_SCHEMA_PATCHES in pet_app/install.py, "
				"keeping patches.txt order."
			)

	def test_install_order_follows_patches_txt(self):
		"""Notification patches extend each other; install must not reorder them."""
		order = _patches_txt_order()
		position = {module: index for index, module in enumerate(order)}

		listed = [
			entry.removesuffix(".execute")
			for entry in INSTALL_SCHEMA_PATCHES
			if entry.removesuffix(".execute") in position
		]
		positions = [position[module] for module in listed]

		self.assertEqual(
			positions,
			sorted(positions),
			"INSTALL_SCHEMA_PATCHES runs patches in a different order than "
			"patches.txt. Later notification patches add fields to doctypes created "
			"by earlier ones, so running them out of order silently loses fields.\n"
			f"install.py order: {listed}",
		)

	def test_install_entries_resolve(self):
		"""Catch a renamed or deleted patch before it breaks a real install."""
		for entry in INSTALL_SCHEMA_PATCHES:
			module_name, _, attribute = entry.rpartition(".")
			with self.subTest(entry=entry):
				try:
					module = importlib.import_module(module_name)
				except ImportError as exc:  # pragma: no cover - failure path
					self.fail(f"{entry} is unimportable: {exc}")
				self.assertTrue(
					callable(getattr(module, attribute, None)),
					f"{entry} does not resolve to a callable.",
				)
