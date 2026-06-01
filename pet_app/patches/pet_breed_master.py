from __future__ import annotations

import frappe
from frappe.utils import cstr


DEFAULT_PET_BREEDS: tuple[dict[str, str], ...] = (
	{"breed_name": "Mixed Breed (Dog)", "animal_species": "Mammal", "animal_type": "Dog"},
	{"breed_name": "Labrador Retriever", "animal_species": "Mammal", "animal_type": "Dog"},
	{"breed_name": "Golden Retriever", "animal_species": "Mammal", "animal_type": "Dog"},
	{"breed_name": "German Shepherd", "animal_species": "Mammal", "animal_type": "Dog"},
	{"breed_name": "Belgian Malinois", "animal_species": "Mammal", "animal_type": "Dog"},
	{"breed_name": "Siberian Husky", "animal_species": "Mammal", "animal_type": "Dog"},
	{"breed_name": "Rottweiler", "animal_species": "Mammal", "animal_type": "Dog"},
	{"breed_name": "Doberman Pinscher", "animal_species": "Mammal", "animal_type": "Dog"},
	{"breed_name": "Pomeranian", "animal_species": "Mammal", "animal_type": "Dog"},
	{"breed_name": "Chihuahua", "animal_species": "Mammal", "animal_type": "Dog"},
	{"breed_name": "Poodle", "animal_species": "Mammal", "animal_type": "Dog"},
	{"breed_name": "Shih Tzu", "animal_species": "Mammal", "animal_type": "Dog"},
	{"breed_name": "Pug", "animal_species": "Mammal", "animal_type": "Dog"},
	{"breed_name": "Beagle", "animal_species": "Mammal", "animal_type": "Dog"},
	{"breed_name": "Cocker Spaniel", "animal_species": "Mammal", "animal_type": "Dog"},
	{"breed_name": "Dachshund", "animal_species": "Mammal", "animal_type": "Dog"},
	{"breed_name": "Pit Bull Terrier", "animal_species": "Mammal", "animal_type": "Dog"},
	{"breed_name": "French Bulldog", "animal_species": "Mammal", "animal_type": "Dog"},
	{"breed_name": "English Bulldog", "animal_species": "Mammal", "animal_type": "Dog"},
	{"breed_name": "Border Collie", "animal_species": "Mammal", "animal_type": "Dog"},
	{"breed_name": "Cane Corso", "animal_species": "Mammal", "animal_type": "Dog"},
	{"breed_name": "Great Dane", "animal_species": "Mammal", "animal_type": "Dog"},
	{"breed_name": "Boxer", "animal_species": "Mammal", "animal_type": "Dog"},
	{"breed_name": "Samoyed", "animal_species": "Mammal", "animal_type": "Dog"},
	{"breed_name": "Saluki", "animal_species": "Mammal", "animal_type": "Dog"},
	{"breed_name": "Kangal", "animal_species": "Mammal", "animal_type": "Dog"},
	{"breed_name": "Anatolian Shepherd", "animal_species": "Mammal", "animal_type": "Dog"},
	{"breed_name": "Mixed Breed (Cat)", "animal_species": "Mammal", "animal_type": "Cat"},
	{"breed_name": "Domestic Shorthair", "animal_species": "Mammal", "animal_type": "Cat"},
	{"breed_name": "Domestic Longhair", "animal_species": "Mammal", "animal_type": "Cat"},
	{"breed_name": "Persian", "animal_species": "Mammal", "animal_type": "Cat"},
	{"breed_name": "British Shorthair", "animal_species": "Mammal", "animal_type": "Cat"},
	{"breed_name": "Scottish Fold", "animal_species": "Mammal", "animal_type": "Cat"},
	{"breed_name": "Siamese", "animal_species": "Mammal", "animal_type": "Cat"},
	{"breed_name": "Maine Coon", "animal_species": "Mammal", "animal_type": "Cat"},
	{"breed_name": "Bengal", "animal_species": "Mammal", "animal_type": "Cat"},
	{"breed_name": "Turkish Angora", "animal_species": "Mammal", "animal_type": "Cat"},
	{"breed_name": "Himalayan", "animal_species": "Mammal", "animal_type": "Cat"},
	{"breed_name": "Russian Blue", "animal_species": "Mammal", "animal_type": "Cat"},
	{"breed_name": "Sphynx", "animal_species": "Mammal", "animal_type": "Cat"},
	{"breed_name": "Ragdoll", "animal_species": "Mammal", "animal_type": "Cat"},
	{"breed_name": "Abyssinian", "animal_species": "Mammal", "animal_type": "Cat"},
	{"breed_name": "Birman", "animal_species": "Mammal", "animal_type": "Cat"},
	{"breed_name": "American Shorthair", "animal_species": "Mammal", "animal_type": "Cat"},
	{"breed_name": "Cockatiel", "animal_species": "Bird", "animal_type": "Bird"},
	{"breed_name": "Budgerigar", "animal_species": "Bird", "animal_type": "Bird"},
	{"breed_name": "Lovebird", "animal_species": "Bird", "animal_type": "Bird"},
	{"breed_name": "African Grey Parrot", "animal_species": "Bird", "animal_type": "Bird"},
	{"breed_name": "Amazon Parrot", "animal_species": "Bird", "animal_type": "Bird"},
	{"breed_name": "Macaw", "animal_species": "Bird", "animal_type": "Bird"},
	{"breed_name": "Canary", "animal_species": "Bird", "animal_type": "Bird"},
	{"breed_name": "Finch", "animal_species": "Bird", "animal_type": "Bird"},
	{"breed_name": "Pigeon", "animal_species": "Bird", "animal_type": "Bird"},
	{"breed_name": "Dove", "animal_species": "Bird", "animal_type": "Bird"},
	{"breed_name": "Mixed Breed (Rabbit)", "animal_species": "Mammal", "animal_type": "Rabbit"},
	{"breed_name": "Holland Lop", "animal_species": "Mammal", "animal_type": "Rabbit"},
	{"breed_name": "Netherland Dwarf", "animal_species": "Mammal", "animal_type": "Rabbit"},
	{"breed_name": "Lionhead", "animal_species": "Mammal", "animal_type": "Rabbit"},
	{"breed_name": "Rex", "animal_species": "Mammal", "animal_type": "Rabbit"},
	{"breed_name": "Mini Rex", "animal_species": "Mammal", "animal_type": "Rabbit"},
	{"breed_name": "Flemish Giant", "animal_species": "Mammal", "animal_type": "Rabbit"},
	{"breed_name": "Angora Rabbit", "animal_species": "Mammal", "animal_type": "Rabbit"},
	{"breed_name": "Goldfish", "animal_species": "Fish", "animal_type": "Fish"},
	{"breed_name": "Betta", "animal_species": "Fish", "animal_type": "Fish"},
	{"breed_name": "Guppy", "animal_species": "Fish", "animal_type": "Fish"},
	{"breed_name": "Molly", "animal_species": "Fish", "animal_type": "Fish"},
	{"breed_name": "Platy", "animal_species": "Fish", "animal_type": "Fish"},
	{"breed_name": "Neon Tetra", "animal_species": "Fish", "animal_type": "Fish"},
	{"breed_name": "Angelfish", "animal_species": "Fish", "animal_type": "Fish"},
	{"breed_name": "Koi", "animal_species": "Fish", "animal_type": "Fish"},
	{"breed_name": "Oscar", "animal_species": "Fish", "animal_type": "Fish"},
	{"breed_name": "Cichlid", "animal_species": "Fish", "animal_type": "Fish"},
	{"breed_name": "Bearded Dragon", "animal_species": "Reptile", "animal_type": "Reptile"},
	{"breed_name": "Leopard Gecko", "animal_species": "Reptile", "animal_type": "Reptile"},
	{"breed_name": "Crested Gecko", "animal_species": "Reptile", "animal_type": "Reptile"},
	{"breed_name": "Ball Python", "animal_species": "Reptile", "animal_type": "Reptile"},
	{"breed_name": "Corn Snake", "animal_species": "Reptile", "animal_type": "Reptile"},
	{"breed_name": "Red-Eared Slider", "animal_species": "Reptile", "animal_type": "Reptile"},
	{"breed_name": "Greek Tortoise", "animal_species": "Reptile", "animal_type": "Reptile"},
	{"breed_name": "Iguana", "animal_species": "Reptile", "animal_type": "Reptile"},
	{"breed_name": "Chameleon", "animal_species": "Reptile", "animal_type": "Reptile"},
	{"breed_name": "Arabian", "animal_species": "Mammal", "animal_type": "Horse"},
	{"breed_name": "Thoroughbred", "animal_species": "Mammal", "animal_type": "Horse"},
	{"breed_name": "Quarter Horse", "animal_species": "Mammal", "animal_type": "Horse"},
	{"breed_name": "Friesian", "animal_species": "Mammal", "animal_type": "Horse"},
	{"breed_name": "Andalusian", "animal_species": "Mammal", "animal_type": "Horse"},
	{"breed_name": "Pony", "animal_species": "Mammal", "animal_type": "Horse"},
	{"breed_name": "Akhal-Teke", "animal_species": "Mammal", "animal_type": "Horse"},
	{"breed_name": "Hamster", "animal_species": "Mammal", "animal_type": "Other"},
	{"breed_name": "Guinea Pig", "animal_species": "Mammal", "animal_type": "Other"},
	{"breed_name": "Ferret", "animal_species": "Mammal", "animal_type": "Other"},
	{"breed_name": "Hedgehog", "animal_species": "Mammal", "animal_type": "Other"},
	{"breed_name": "Unknown / Not Listed", "animal_species": "", "animal_type": "Other"},
)


def execute():
	if not frappe.db.exists("DocType", "Pet Breed"):
		return

	for row in DEFAULT_PET_BREEDS:
		ensure_pet_breed(**row)

	for row in _existing_pet_breed_rows():
		ensure_pet_breed(
			breed_name=row["breed"],
			animal_species=row.get("animal_species"),
			animal_type=row.get("animal_type"),
			custom_notes="Created from existing Pet.breed value during migration.",
		)


def ensure_pet_breed(
	breed_name: str,
	animal_species: str | None = None,
	animal_type: str | None = None,
	custom_notes: str | None = None,
) -> str | None:
	breed_name = cstr(breed_name).strip()
	if not breed_name:
		return None

	if frappe.db.exists("Pet Breed", breed_name):
		doc = frappe.get_doc("Pet Breed", breed_name)
		updates = {}
		if animal_species and not doc.get("animal_species"):
			updates["animal_species"] = animal_species
		if animal_type and not doc.get("animal_type"):
			updates["animal_type"] = animal_type
		if not doc.get("enabled"):
			updates["enabled"] = 1
		if updates:
			doc.update(updates)
			doc.save(ignore_permissions=True)
		return doc.name

	doc = frappe.get_doc(
		{
			"doctype": "Pet Breed",
			"breed_name": breed_name,
			"enabled": 1,
			"animal_species": animal_species,
			"animal_type": animal_type,
			"notes": custom_notes,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def _existing_pet_breed_rows() -> list[dict[str, str]]:
	if not frappe.db.exists("DocType", "Pet"):
		return []

	seen = set()
	rows = []
	for row in frappe.get_all(
		"Pet",
		fields=["breed", "animal_species", "animal_type"],
		ignore_permissions=True,
	):
		breed = cstr(row.get("breed")).strip()
		if not breed or breed in seen:
			continue
		seen.add(breed)
		rows.append(
			{
				"breed": breed,
				"animal_species": cstr(row.get("animal_species")).strip(),
				"animal_type": cstr(row.get("animal_type")).strip(),
			}
		)
	return rows
