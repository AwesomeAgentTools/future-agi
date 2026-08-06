import structlog

from agentic_eval.core_evals.run_prompt.litellm_models import LiteLLMModelManager
from model_hub.models.choices import ProviderLogoUrls

logger = structlog.get_logger(__name__)


def get_provider_for_model(
    model_name: str, organization_id: str = None, workspace_id: str = None
) -> str | None:
    """Get the provider name for a given model. Returns None for unavailable models."""
    try:
        model_manager = LiteLLMModelManager(
            model_name=model_name, organization_id=organization_id
        )
        return model_manager.get_provider(
            model_name=model_name,
            organization_id=organization_id,
            workspace_id=workspace_id,
        )
    except ValueError:
        logger.warning("provider_lookup_failed", model_name=model_name)
        return None


def get_provider_logo_url(
    model_name: str, organization_id: str = None, workspace_id: str = None
) -> str | None:
    """Get the provider logo URL for a given model."""
    provider = get_provider_for_model(model_name, organization_id, workspace_id)
    if not provider:
        return None
    return ProviderLogoUrls.get_url_by_provider(provider)


def is_model_in_catalog(model_name: str, organization_id=None) -> bool:
    """Check if a model name exists in the catalog (including custom models)."""
    from agentic_eval.core_evals.run_prompt.available_models import AVAILABLE_MODELS

    if any(m["model_name"] == model_name for m in AVAILABLE_MODELS):
        return True
    if organization_id:
        from model_hub.models.custom_models import CustomAIModel

        return CustomAIModel.objects.filter(
            organization_id=organization_id,
            user_model_id=model_name,
            deleted=False,
        ).exists()
    return False
