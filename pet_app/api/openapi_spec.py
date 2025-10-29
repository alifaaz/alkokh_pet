# Copyright (c) 2025, solvers and contributors
# For license information, please see license.txt

"""
OpenAPI specification for Pet API endpoints
"""

import frappe


def get_openapi_spec():
	"""Generate OpenAPI 3.0 specification for Pet API"""

	site_url = frappe.utils.get_url()

	spec = {
		"openapi": "3.0.0",
		"info": {
			"title": "Pet Management API",
			"version": "1.0.0",
			"description": "API endpoints for managing pets, including CRUD operations, medical records, and adoption management.",
			"contact": {
				"name": "API Support",
				"email": "support@example.com"
			}
		},
		"servers": [
			{
				"url": f"{site_url}/api/method",
				"description": "Production server"
			}
		],
		"tags": [
			{
				"name": "Pets",
				"description": "Pet management operations"
			}
		],
		"paths": {
			"/pet_app.api.pet_api.get_available_pets": {
				"get": {
					"tags": ["Pets"],
					"summary": "Get available pets for adoption",
					"responses": {"200": {"description": "OK", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/PetListResponse"}}}}}
				}
			},
			"/pet_app.api.pet_api.get_pet_statistics": {
				"get": {
					"tags": ["Pets"],
					"summary": "Get pet statistics",
					"responses": {"200": {"description": "OK", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/StatisticsResponse"}}}}}
				}
			},
			"/pet_app.api.pet_api.get_all_pets": {
				"get": {
					"tags": ["Pets"],
					"summary": "List pets (paginated)",
					"parameters": [
						{"name": "limit", "in": "query", "schema": {"type": "integer", "default": 20}},
						{"name": "offset", "in": "query", "schema": {"type": "integer", "default": 0}},
						{"name": "order_by", "in": "query", "schema": {"type": "string", "default": "modified desc"}}
					],
					"responses": {"200": {"description": "OK", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/PaginatedPetListResponse"}}}}},
					"security": [{"api_key": []}, {"api_secret": []}]
				}
			},
			"/pet_app.api.pet_api.get_pet_details": {
				"get": {
					"tags": ["Pets"],
					"summary": "Get pet details",
					"parameters": [{"name": "pet_id", "in": "query", "required": True, "schema": {"type": "string"}}],
					"responses": {"200": {"description": "OK", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/PetDetailResponse"}}}}},
					"security": [{"api_key": []}, {"api_secret": []}]
				}
			},
			"/pet_app.api.pet_api.get_pets_by_owner": {
				"get": {
					"tags": ["Pets"],
					"summary": "Get pets by owner",
					"parameters": [{"name": "owner", "in": "query", "required": True, "schema": {"type": "string"}}],
					"responses": {"200": {"description": "OK", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/PetListResponse"}}}}},
					"security": [{"api_key": []}, {"api_secret": []}]
				}
			},
			"/pet_app.api.pet_api.get_pets_by_species": {
				"get": {
					"tags": ["Pets"],
					"summary": "Get pets by species",
					"parameters": [{"name": "species", "in": "query", "required": True, "schema": {"type": "string"}}],
					"responses": {"200": {"description": "OK", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/PetListResponse"}}}}},
					"security": [{"api_key": []}, {"api_secret": []}]
				}
			},
			"/pet_app.api.pet_api.search_pets": {
				"get": {
					"tags": ["Pets"],
					"summary": "Search pets",
					"parameters": [
						{"name": "query", "in": "query", "required": True, "schema": {"type": "string"}},
						{"name": "filters", "in": "query", "schema": {"type": "string"}, "description": "JSON string of additional filters"}
					],
					"responses": {"200": {"description": "OK", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/SearchResponse"}}}}},
					"security": [{"api_key": []}, {"api_secret": []}]
				}
			},
			"/pet_app.api.pet_api.filter_pets": {
				"get": {
					"tags": ["Pets"],
					"summary": "Filter pets",
					"parameters": [
						{"name": "species", "in": "query", "schema": {"type": "string"}},
						{"name": "status", "in": "query", "schema": {"type": "string"}},
						{"name": "gender", "in": "query", "schema": {"type": "string"}},
						{"name": "vaccination_status", "in": "query", "schema": {"type": "string"}}
					],
					"responses": {"200": {"description": "OK", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/SearchResponse"}}}}},
					"security": [{"api_key": []}, {"api_secret": []}]
				}
			},
			"/pet_app.api.pet_api.get_pets_needing_medical_attention": {
				"get": {
					"tags": ["Pets"],
					"summary": "Get pets needing medical attention",
					"responses": {"200": {"description": "OK", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/MedicalAttentionResponse"}}}}},
					"security": [{"api_key": []}, {"api_secret": []}]
				}
			},
			"/pet_app.api.pet_api.create_pet": {
				"post": {
					"tags": ["Pets"],
					"summary": "Create a new pet",
					"description": "Create a new pet record.",
					"requestBody": {
						"required": False,
						"content": {
							"application/json": {"schema": {"$ref": "#/components/schemas/PetCreate"}}
						}
					},
					"responses": {
						"200": {"description": "Pet created successfully", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/PetDetailResponse"}}}}
					},
					"security": [{"api_key": []}, {"api_secret": []}]
				}
			},
			"/pet_app.api.pet_api.update_pet": {
				"put": {
					"tags": ["Pets"],
					"summary": "Update a pet (full update)",
					"description": "Update an existing pet. Provide pet_id and fields via query or JSON body.",
					"parameters": [
						{"name": "pet_id", "in": "query", "required": True, "schema": {"type": "string"}}
					],
					"requestBody": {
						"required": False,
						"content": {"application/json": {"schema": {"$ref": "#/components/schemas/PetUpdate"}}}
					},
					"responses": {
						"200": {"description": "Pet updated successfully", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/PetDetailResponse"}}}}
					},
					"security": [{"api_key": []}, {"api_secret": []}]
				}
			},
			"/pet_app.api.pet_api.delete_pet": {
				"delete": {
					"tags": ["Pets"],
					"summary": "Delete a pet",
					"parameters": [
						{"name": "pet_id", "in": "query", "required": True, "schema": {"type": "string"}}
					],
					"responses": {
						"200": {"description": "Pet deleted", "content": {"application/json": {"schema": {"type": "object", "properties": {"success": {"type": "boolean"}, "message": {"type": "string"}}}}}}
					},
					"security": [{"api_key": []}, {"api_secret": []}]
				}
			},
			"/pet_app.api.pet_api.get_csrf_token": {
				"get": {
					"tags": ["Pets"],
					"summary": "Get CSRF token for session",
					"description": "Returns CSRF token if authenticated session exists.",
					"responses": {"200": {"description": "CSRF token response", "content": {"application/json": {"schema": {"type": "object", "properties": {"success": {"type": "boolean"}, "csrf_token": {"type": "string"}}}}}}}
				}
			},
			"/pet_app.api.pet_api.update_medical_info": {
				"post": {
					"tags": ["Pets"],
					"summary": "Update medical information",
					"parameters": [
						{"name": "pet_id", "in": "query", "required": True, "schema": {"type": "string"}},
						{"name": "vaccination_status", "in": "query", "schema": {"type": "string"}},
						{"name": "last_vet_visit", "in": "query", "schema": {"type": "string", "format": "date"}},
						{"name": "next_vet_visit", "in": "query", "schema": {"type": "string", "format": "date"}},
						{"name": "medical_notes", "in": "query", "schema": {"type": "string"}}
					],
					"responses": {"200": {"description": "OK", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/PetDetailResponse"}}}}},
					"security": [{"api_key": []}, {"api_secret": []}]
				}
			},
			"/pet_app.api.pet_api.update_pet_status": {
				"post": {
					"tags": ["Pets"],
					"summary": "Update pet status",
					"parameters": [
						{"name": "pet_id", "in": "query", "required": True, "schema": {"type": "string"}},
						{"name": "status", "in": "query", "required": True, "schema": {"type": "string"}}
					],
					"responses": {"200": {"description": "OK", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/PetDetailResponse"}}}}},
					"security": [{"api_key": []}, {"api_secret": []}]
				}
			},
		},
		"components": {
			"schemas": {
				"PetCreate": {
					"type": "object",
					"required": ["pet_name", "species", "status"],
					"properties": {
						"pet_name": {"type": "string"},
						"species": {"type": "string", "enum": ["Dog", "Cat", "Bird", "Fish", "Rabbit", "Hamster", "Guinea Pig", "Reptile", "Other"]},
						"breed": {"type": "string"},
						"gender": {"type": "string", "enum": ["Male", "Female", "Unknown"]},
						"status": {"type": "string", "enum": ["Active", "Adopted", "Available for Adoption", "Medical Care", "Inactive", "Deceased"]},
						"image": {"type": "string"},
						"color": {"type": "string"},
						"microchip_number": {"type": "string"},
						"date_of_birth": {"type": "string", "format": "date"},
						"weight": {"type": "number"},
						"height": {"type": "number"},
						"distinctive_marks": {"type": "string"},
						"owner": {"type": "string"},
						"owner_name": {"type": "string"},
						"owner_contact": {"type": "string"},
						"acquisition_date": {"type": "string", "format": "date"},
						"adoption_date": {"type": "string", "format": "date"},
						"vaccination_status": {"type": "string", "enum": ["Up to Date", "Due Soon", "Overdue", "Not Vaccinated"]},
						"last_vet_visit": {"type": "string", "format": "date"},
						"next_vet_visit": {"type": "string", "format": "date"},
						"is_neutered": {"type": "boolean"},
						"allergies": {"type": "string"},
						"medical_notes": {"type": "string"},
						"description": {"type": "string"},
						"special_needs": {"type": "string"},
						"behavioral_notes": {"type": "string"}
					}
				},
				"PetUpdate": {
					"type": "object",
					"properties": {
						"pet_name": {"type": "string"},
						"species": {"type": "string", "enum": ["Dog", "Cat", "Bird", "Fish", "Rabbit", "Hamster", "Guinea Pig", "Reptile", "Other"]},
						"breed": {"type": "string"},
						"gender": {"type": "string", "enum": ["Male", "Female", "Unknown"]},
						"status": {"type": "string", "enum": ["Active", "Adopted", "Available for Adoption", "Medical Care", "Inactive", "Deceased"]},
						"image": {"type": "string"},
						"color": {"type": "string"},
						"microchip_number": {"type": "string"},
						"date_of_birth": {"type": "string", "format": "date"},
						"weight": {"type": "number"},
						"height": {"type": "number"},
						"distinctive_marks": {"type": "string"},
						"owner": {"type": "string"},
						"owner_name": {"type": "string"},
						"owner_contact": {"type": "string"},
						"acquisition_date": {"type": "string", "format": "date"},
						"adoption_date": {"type": "string", "format": "date"},
						"vaccination_status": {"type": "string", "enum": ["Up to Date", "Due Soon", "Overdue", "Not Vaccinated"]},
						"last_vet_visit": {"type": "string", "format": "date"},
						"next_vet_visit": {"type": "string", "format": "date"},
						"is_neutered": {"type": "boolean"},
						"allergies": {"type": "string"},
						"medical_notes": {"type": "string"},
						"description": {"type": "string"},
						"special_needs": {"type": "string"},
						"behavioral_notes": {"type": "string"}
					},
					"additionalProperties": True
				},
				"Pet": {
					"type": "object",
					"properties": {
						"name": {"type": "string", "example": "PET-00001"},
						"pet_name": {"type": "string", "example": "Max"},
						"species": {"type": "string", "example": "Dog"},
						"breed": {"type": "string", "example": "Golden Retriever"},
						"age": {"type": "string", "example": "3 years, 2 months"},
						"gender": {"type": "string", "example": "Male"},
						"status": {"type": "string", "example": "Active"},
						"owner_name": {"type": "string", "example": "John Doe"},
						"owner_contact": {"type": "string", "example": "+1234567890"},
						"image": {"type": "string", "example": "/files/pet_image.jpg"},
						"color": {"type": "string", "example": "Golden"},
						"weight": {"type": "number", "example": 30.5},
						"height": {"type": "number", "example": 60.0},
						"microchip_number": {"type": "string", "example": "ABC123456789"},
						"date_of_birth": {"type": "string", "format": "date", "example": "2020-05-15"},
						"vaccination_status": {"type": "string", "example": "Up to Date"},
						"is_neutered": {"type": "boolean", "example": True},
						"description": {"type": "string"},
						"medical_notes": {"type": "string"},
						"special_needs": {"type": "string"}
					}
				},
				"PetListResponse": {
					"type": "object",
					"properties": {
						"success": {"type": "boolean", "example": True},
						"data": {
							"type": "array",
							"items": {"$ref": "#/components/schemas/Pet"}
						}
					}
				},
				"PetDetailResponse": {
					"type": "object",
					"properties": {
						"success": {"type": "boolean", "example": True},
						"message": {"type": "string"},
						"data": {"$ref": "#/components/schemas/Pet"}
					}
				},
				"SearchResponse": {
					"type": "object",
					"properties": {
						"success": {"type": "boolean", "example": True},
						"data": {
							"type": "array",
							"items": {"$ref": "#/components/schemas/Pet"}
						},
						"count": {"type": "integer", "example": 10}
					}
				},
				"StatisticsResponse": {
					"type": "object",
					"properties": {
						"success": {"type": "boolean", "example": True},
						"data": {
							"type": "object",
							"properties": {
								"total_pets": {"type": "integer", "example": 150},
								"by_species": {
									"type": "array",
									"items": {
										"type": "object",
										"properties": {
											"species": {"type": "string"},
											"count": {"type": "integer"}
										}
									}
								},
								"by_status": {
									"type": "array",
									"items": {
										"type": "object",
										"properties": {
											"status": {"type": "string"},
											"count": {"type": "integer"}
										}
									}
								},
								"by_gender": {
									"type": "array",
									"items": {
										"type": "object",
										"properties": {
											"gender": {"type": "string"},
											"count": {"type": "integer"}
										}
									}
								},
								"vaccination_stats": {
									"type": "array",
									"items": {
										"type": "object",
										"properties": {
											"vaccination_status": {"type": "string"},
											"count": {"type": "integer"}
										}
									}
								}
							}
						}
					}
				},
				"MedicalAttentionResponse": {
					"type": "object",
					"properties": {
						"success": {"type": "boolean", "example": True},
						"data": {
							"type": "object",
							"properties": {
								"overdue_vaccinations": {
									"type": "array",
									"items": {"$ref": "#/components/schemas/Pet"}
								},
								"overdue_vet_visits": {
									"type": "array",
									"items": {"$ref": "#/components/schemas/Pet"}
								},
								"in_medical_care": {
									"type": "array",
									"items": {"$ref": "#/components/schemas/Pet"}
								}
							}
						}
					}
				},
				"PaginatedPetListResponse": {
					"type": "object",
					"properties": {
						"success": {"type": "boolean", "example": True},
						"data": {"type": "array", "items": {"$ref": "#/components/schemas/Pet"}},
						"count": {"type": "integer", "example": 20},
						"total": {"type": "integer", "example": 200},
						"limit": {"type": "integer", "example": 20},
						"offset": {"type": "integer", "example": 0}
					}
				}
			},
			"securitySchemes": {
				"api_key": {
					"type": "apiKey",
					"in": "header",
					"name": "Authorization",
					"description": "Frappe API Key in format: 'token api_key:api_secret'"
				},
				"api_secret": {
					"type": "apiKey",
					"in": "header",
					"name": "Authorization",
					"description": "Frappe API Secret"
				}
			}
		}
	}

	return spec


@frappe.whitelist(allow_guest=True)
def get_openapi_json():
	"""Return OpenAPI specification as JSON"""
	return get_openapi_spec()
