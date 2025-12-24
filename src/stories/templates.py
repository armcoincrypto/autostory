"""
Story Templates - Manage story content templates
"""
import random
from datetime import datetime
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, field
import structlog

import sys
sys.path.insert(0, '/home/user/autostory')
from src.core.models import Campaign
from src.core.database import get_db_context

logger = structlog.get_logger(__name__)


@dataclass
class StoryTemplate:
    """Template for story content"""
    id: str
    name: str
    caption: str
    media_paths: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    variables: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)

    def render(self, context: Optional[Dict[str, Any]] = None) -> str:
        """Render caption with variable substitution"""
        rendered = self.caption

        # Merge default variables with provided context
        all_vars = {**self.variables, **(context or {})}

        for key, value in all_vars.items():
            placeholder = f"{{{key}}}"
            rendered = rendered.replace(placeholder, str(value))

        return rendered

    def get_random_media(self) -> Optional[str]:
        """Get a random media file from template"""
        if self.media_paths:
            return random.choice(self.media_paths)
        return None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            "id": self.id,
            "name": self.name,
            "caption": self.caption,
            "media_paths": self.media_paths,
            "tags": self.tags,
            "variables": self.variables,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StoryTemplate":
        """Create from dictionary"""
        return cls(
            id=data["id"],
            name=data["name"],
            caption=data["caption"],
            media_paths=data.get("media_paths", []),
            tags=data.get("tags", []),
            variables=data.get("variables", {}),
            created_at=datetime.fromisoformat(data["created_at"]) if "created_at" in data else datetime.utcnow(),
        )


class TemplateManager:
    """
    Manage story templates

    Features:
    - CRUD operations
    - Template categories
    - Random selection
    - Variable substitution
    """

    def __init__(self):
        self._templates: Dict[str, StoryTemplate] = {}
        self._load_default_templates()

    def _load_default_templates(self) -> None:
        """Load default templates"""
        defaults = [
            StoryTemplate(
                id="promo_1",
                name="Simple Promo",
                caption="Check this out! 🔥\n\n{message}",
                tags=["promo", "simple"],
            ),
            StoryTemplate(
                id="announcement_1",
                name="Announcement",
                caption="📢 {title}\n\n{description}\n\n👉 Learn more",
                tags=["announcement"],
            ),
            StoryTemplate(
                id="question_1",
                name="Engagement Question",
                caption="What do you think? 🤔\n\n{question}\n\nDrop your thoughts below! 👇",
                tags=["engagement", "question"],
            ),
            StoryTemplate(
                id="share_1",
                name="Content Share",
                caption="Sharing something special with you! ✨\n\n{content}",
                tags=["share"],
            ),
        ]

        for template in defaults:
            self._templates[template.id] = template

    def add_template(self, template: StoryTemplate) -> bool:
        """Add a new template"""
        if template.id in self._templates:
            logger.warning("Template already exists", template_id=template.id)
            return False

        self._templates[template.id] = template
        logger.info("Template added", template_id=template.id)
        return True

    def update_template(self, template_id: str, updates: Dict[str, Any]) -> Optional[StoryTemplate]:
        """Update an existing template"""
        if template_id not in self._templates:
            return None

        template = self._templates[template_id]

        for key, value in updates.items():
            if hasattr(template, key):
                setattr(template, key, value)

        return template

    def delete_template(self, template_id: str) -> bool:
        """Delete a template"""
        if template_id in self._templates:
            del self._templates[template_id]
            logger.info("Template deleted", template_id=template_id)
            return True
        return False

    def get_template(self, template_id: str) -> Optional[StoryTemplate]:
        """Get a template by ID"""
        return self._templates.get(template_id)

    def list_templates(self, tag: Optional[str] = None) -> List[StoryTemplate]:
        """List all templates, optionally filtered by tag"""
        templates = list(self._templates.values())

        if tag:
            templates = [t for t in templates if tag in t.tags]

        return templates

    def get_random_template(self, tag: Optional[str] = None) -> Optional[StoryTemplate]:
        """Get a random template"""
        templates = self.list_templates(tag)
        if templates:
            return random.choice(templates)
        return None

    def render_template(
        self,
        template_id: str,
        context: Optional[Dict[str, Any]] = None
    ) -> Optional[str]:
        """Render a template with context"""
        template = self.get_template(template_id)
        if template:
            return template.render(context)
        return None

    def export_templates(self) -> List[Dict[str, Any]]:
        """Export all templates as dictionaries"""
        return [t.to_dict() for t in self._templates.values()]

    def import_templates(self, templates_data: List[Dict[str, Any]]) -> int:
        """Import templates from list of dictionaries"""
        imported = 0
        for data in templates_data:
            try:
                template = StoryTemplate.from_dict(data)
                self._templates[template.id] = template
                imported += 1
            except Exception as e:
                logger.error("Failed to import template", error=str(e), data=data)

        logger.info("Templates imported", count=imported)
        return imported


# Global template manager instance
template_manager = TemplateManager()
