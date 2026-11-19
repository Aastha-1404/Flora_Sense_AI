import json
from pathlib import Path

from streamlit.testing.v1 import AppTest

from plant_data import (
    UNAVAILABLE_BOTANICAL_INFO,
    get_detailed_plant_profile,
    plant_reference_search_url,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_FIELDS = (
    "common_name",
    "scientific_name",
    "family",
    "short_description",
    "botanical_description",
    "plant_type",
    "native_region",
    "sunlight",
    "watering",
    "soil",
    "temperature",
    "humidity",
    "maintenance",
    "propagation",
    "pruning",
    "pet_safety",
    "reference_sources",
)


def mapping_labels(mapping_dir):
    path = PROJECT_ROOT / mapping_dir / "class_mapping.json"
    with path.open(encoding="utf-8") as mapping_file:
        mapping = json.load(mapping_file)["id2label"]
    return [mapping[str(index)] for index in range(len(mapping))]


def test_every_checkpoint_class_has_a_complete_local_profile():
    for mapping_dir, expected_count in (("checkpoints_47", 47), ("checkpoints", 19)):
        labels = mapping_labels(mapping_dir)
        assert len(labels) == expected_count
        for label in labels:
            profile = get_detailed_plant_profile(label)
            assert profile is not None, label
            assert all(profile.get(field) for field in REQUIRED_FIELDS), label
            assert profile["short_description"] != UNAVAILABLE_BOTANICAL_INFO
            assert profile["botanical_description"] != UNAVAILABLE_BOTANICAL_INFO
            assert profile["reference_sources"][0]["url"].startswith(
                "https://www.rhs.org.uk/plants/search-results?"
            )


def test_broad_model_labels_do_not_claim_one_species():
    assert get_detailed_plant_profile("Elephant Ear (Alocasia spp.)")["scientific_name"] == "Alocasia spp."
    assert get_detailed_plant_profile("Chinese Evergreen")["scientific_name"] == "Aglaonema spp."
    assert get_detailed_plant_profile("Calathea")["scientific_name"].startswith("Goeppertia spp.")


def test_reference_search_uses_rhs_and_plant_identity():
    url = plant_reference_search_url("Snake Plant", "Dracaena trifasciata")
    assert url.startswith("https://www.rhs.org.uk/plants/search-results?")
    assert "Dracaena+trifasciata" in url


def test_supported_plant_detail_and_back_navigation():
    app = AppTest.from_file(str(PROJECT_ROOT / "app.py")).run(timeout=60)
    app.radio[0].set_value(app.radio[0].options[3]).run(timeout=60)
    assert not app.exception

    next(button for button in app.button if button.key == "view_plant_details_1").click().run(timeout=60)
    assert not app.exception
    assert any("Botanical description" in element.value for element in app.markdown)
    assert any(button.key == "back_to_supported_plants" for button in app.button)

    next(button for button in app.button if button.key == "back_to_supported_plants").click().run(timeout=60)
    assert not app.exception
    assert len([button for button in app.button if button.label == "View details"]) == 47


def test_supported_plant_search_includes_aliases():
    app = AppTest.from_file(str(PROJECT_ROOT / "app.py")).run(timeout=60)
    app.radio[0].set_value(app.radio[0].options[3]).run(timeout=60)
    app.text_input[0].set_value("devils ivy").run(timeout=60)
    assert not app.exception
    assert any("Showing 1 of 47" in element.value for element in app.caption)
    assert any("Epipremnum aureum" in element.value for element in app.markdown)
    assert any(button.key == "view_plant_details_36" for button in app.button)


def test_supported_plant_search_includes_common_and_scientific_names():
    app = AppTest.from_file(str(PROJECT_ROOT / "app.py")).run(timeout=60)
    app.radio[0].set_value(app.radio[0].options[3]).run(timeout=60)

    app.text_input[0].set_value("Aloe Vera").run(timeout=60)
    assert any("Aloe Vera" in element.value for element in app.markdown)
    assert len([button for button in app.button if button.label == "View details"]) == 1

    app.text_input[0].set_value("Dracaena trifasciata").run(timeout=60)
    assert any("Snake Plant" in element.value for element in app.markdown)
    assert any("Dracaena trifasciata" in element.value for element in app.markdown)
    assert len([button for button in app.button if button.label == "View details"]) == 1
