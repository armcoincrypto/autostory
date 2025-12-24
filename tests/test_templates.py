"""
Tests for Story Templates
"""
import pytest
from datetime import datetime

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.stories.templates import StoryTemplate, TemplateManager


class TestStoryTemplate:
    """Tests for StoryTemplate class"""

    @pytest.fixture
    def sample_template(self):
        """Create a sample template"""
        return StoryTemplate(
            id="test_1",
            name="Test Template",
            caption="Hello {name}! Check out {product}.",
            media_paths=["/path/to/media1.jpg", "/path/to/media2.jpg"],
            tags=["promo", "test"],
            variables={"name": "User", "product": "our app"}
        )

    def test_render_with_defaults(self, sample_template):
        """Test rendering with default variables"""
        result = sample_template.render()
        assert "Hello User!" in result
        assert "Check out our app" in result

    def test_render_with_context(self, sample_template):
        """Test rendering with custom context"""
        result = sample_template.render({"name": "John", "product": "STORYFLEET"})
        assert "Hello John!" in result
        assert "Check out STORYFLEET" in result

    def test_get_random_media(self, sample_template):
        """Test getting random media"""
        media = sample_template.get_random_media()
        assert media in sample_template.media_paths

    def test_get_random_media_empty(self):
        """Test getting random media when empty"""
        template = StoryTemplate(id="empty", name="Empty", caption="Test")
        assert template.get_random_media() is None

    def test_to_dict(self, sample_template):
        """Test converting to dictionary"""
        data = sample_template.to_dict()

        assert data["id"] == "test_1"
        assert data["name"] == "Test Template"
        assert len(data["media_paths"]) == 2
        assert "promo" in data["tags"]

    def test_from_dict(self):
        """Test creating from dictionary"""
        data = {
            "id": "from_dict",
            "name": "From Dict",
            "caption": "Created from dict",
            "media_paths": ["/path.jpg"],
            "tags": ["test"],
            "variables": {},
            "created_at": datetime.utcnow().isoformat()
        }

        template = StoryTemplate.from_dict(data)

        assert template.id == "from_dict"
        assert template.name == "From Dict"


class TestTemplateManager:
    """Tests for TemplateManager class"""

    @pytest.fixture
    def manager(self):
        """Create a template manager"""
        return TemplateManager()

    def test_default_templates_loaded(self, manager):
        """Test that default templates are loaded"""
        templates = manager.list_templates()
        assert len(templates) > 0

    def test_add_template(self, manager):
        """Test adding a template"""
        template = StoryTemplate(
            id="new_template",
            name="New Template",
            caption="New caption"
        )

        result = manager.add_template(template)
        assert result == True

        retrieved = manager.get_template("new_template")
        assert retrieved is not None
        assert retrieved.name == "New Template"

    def test_add_duplicate_template(self, manager):
        """Test adding duplicate template fails"""
        template = StoryTemplate(id="dup", name="Dup", caption="Dup")
        manager.add_template(template)

        result = manager.add_template(template)
        assert result == False

    def test_update_template(self, manager):
        """Test updating a template"""
        template = StoryTemplate(id="update_test", name="Original", caption="Original")
        manager.add_template(template)

        updated = manager.update_template("update_test", {"name": "Updated"})

        assert updated is not None
        assert updated.name == "Updated"

    def test_delete_template(self, manager):
        """Test deleting a template"""
        template = StoryTemplate(id="delete_test", name="Delete", caption="Delete")
        manager.add_template(template)

        result = manager.delete_template("delete_test")
        assert result == True

        assert manager.get_template("delete_test") is None

    def test_list_templates_with_tag(self, manager):
        """Test listing templates by tag"""
        template1 = StoryTemplate(id="tag1", name="Tag1", caption="T1", tags=["special"])
        template2 = StoryTemplate(id="tag2", name="Tag2", caption="T2", tags=["other"])
        manager.add_template(template1)
        manager.add_template(template2)

        special = manager.list_templates(tag="special")
        assert len(special) == 1
        assert special[0].id == "tag1"

    def test_get_random_template(self, manager):
        """Test getting random template"""
        template = manager.get_random_template()
        assert template is not None

    def test_render_template(self, manager):
        """Test rendering template through manager"""
        template = StoryTemplate(
            id="render_test",
            name="Render",
            caption="Hello {name}"
        )
        manager.add_template(template)

        result = manager.render_template("render_test", {"name": "World"})
        assert result == "Hello World"

    def test_export_import(self, manager):
        """Test exporting and importing templates"""
        # Add some templates
        for i in range(3):
            manager.add_template(StoryTemplate(
                id=f"export_{i}",
                name=f"Export {i}",
                caption=f"Caption {i}"
            ))

        # Export
        exported = manager.export_templates()
        assert len(exported) >= 3

        # Create new manager and import
        new_manager = TemplateManager()
        imported_count = new_manager.import_templates(exported)
        assert imported_count == len(exported)
