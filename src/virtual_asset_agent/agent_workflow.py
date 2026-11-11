"""LangChain-powered agent workflow for AI-assisted regulatory analysis.

Supports both local Ollama models and OpenAI-compatible chat APIs using the same
contract-driven schema. The workflow is intentionally conservative: if no AI
configuration is present, it falls back to a local structured summary instead of
crashing the UI or ingestion pipeline.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field

try:
    from langchain_ollama import ChatOllama
except ImportError:  # pragma: no cover - fallback for older LangChain installs
    try:
        from langchain_community.chat_models import ChatOllama
    except ImportError:  # pragma: no cover - optional dependency for local LLMs
        ChatOllama = None

try:
    from langchain_openai import ChatOpenAI
except ImportError:  # pragma: no cover - optional dependency for provider APIs
    ChatOpenAI = None


load_dotenv(Path(__file__).resolve().parents[2] / '.env')


class ExecutiveSummary(BaseModel):
    """Normalized JSON schema for a regulatory summary."""

    executive_summary: str = Field(..., description='A short executive summary of the regulatory development.')
    impact_tags: list[str] = Field(default_factory=list, description='Impact tags such as reserve, custody, cross-border, or stablecoin.')
    risk_rating: Literal['low', 'medium', 'high', 'critical', 'unknown'] = 'unknown'
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    key_findings: list[str] = Field(default_factory=list)
    source_citations: list[str] = Field(default_factory=list)


class CriticReview(BaseModel):
    """Critic agent output checking a secondary claim against primary legal text."""

    claim_status: Literal['supported', 'unsupported', 'mixed', 'unknown'] = 'unknown'
    summary: str = Field(..., description='Assessment of whether the claim is substantiated by the primary source.')
    discrepancies: list[str] = Field(default_factory=list)
    supporting_evidence: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class AgentWorkflowResult(BaseModel):
    """Combined result from analysis + critic review."""

    jurisdiction: str = 'unknown'
    asset_class: str = 'unknown'
    executive_summary: str = ''
    impact_tags: list[str] = Field(default_factory=list)
    risk_rating: Literal['low', 'medium', 'high', 'critical', 'unknown'] = 'unknown'
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    critic_review: CriticReview


def get_agent_settings() -> dict[str, Any]:
    """Read AI config from environment variables; values are single place to change."""
    provider = os.getenv('AI_PROVIDER', 'openai_compatible').strip().lower()
    if provider not in {'ollama', 'openai_compatible'}:
        provider = 'openai_compatible'
    return {
        'provider': provider,
        'openai_base_url': os.getenv('OPENAI_COMPATIBLE_BASE_URL', 'http://localhost:11434/v1'),
        'openai_model': os.getenv('OPENAI_COMPATIBLE_MODEL', 'gpt-4o-mini'),
        'openai_api_key': os.getenv('OPENAI_COMPATIBLE_API_KEY', 'ollama'),
        'temperature': float(os.getenv('AI_TEMPERATURE', '0.2')),
        'timeout': int(os.getenv('AI_TIMEOUT_SECONDS', '60')),
        'enabled': os.getenv('AI_ENABLED', 'true').strip().lower() in {'1', 'true', 'yes', 'on'},
    }


def build_llm():
    """Build a LangChain chat model for Ollama or an OpenAI-compatible proxy."""
    settings = get_agent_settings()
    if not settings['enabled']:
        return None
    provider = settings['provider']
    if provider == 'ollama':
        if ChatOllama is None:
            raise ImportError('ChatOllama support is missing. Install langchain-ollama or langchain-community with Ollama support.')
        return ChatOllama(
            model=settings['ollama_model'],
            base_url=settings['ollama_base_url'],
            temperature=settings['temperature'],
            timeout=settings['timeout'],
        )
    if ChatOpenAI is None:
        raise ImportError('langchain-openai is missing. Install the AI requirements.')
    return ChatOpenAI(
        model=settings['openai_model'],
        base_url=settings['openai_base_url'],
        api_key=settings['openai_api_key'],
        temperature=settings['temperature'],
        timeout=settings['timeout'],
    )


def _fallback_analysis(raw_text: str, title: str = '', jurisdiction: str = 'unknown', asset_class: str = 'unknown') -> dict[str, Any]:
    text = (raw_text or '').strip()
    lower = text.lower()
    tags = []
    if 'stablecoin' in lower:
        tags.append('stablecoin')
    if 'reserve' in lower:
        tags.append('reserve')
    if 'custody' in lower:
        tags.append('custody')
    if 'cross-border' in lower or 'cross border' in lower:
        tags.append('cross-border')
    if 'token' in lower and 'security' in lower:
        tags.append('tokenized_security')
    if 'rwa' in lower:
        tags.append('rwa')
    if 'cbdc' in lower:
        tags.append('cbdc')
    if not tags:
        tags = ['regulatory_update']

    risk = 'medium'
    if any(term in lower for term in ['ban', 'prohibit', 'restriction', 'suspend']):
        risk = 'high'
    if len(text) < 500:
        risk = 'low'

    summary = (title or 'Regulatory material').strip() or 'Regulatory material'
    return {
        'jurisdiction': jurisdiction,
        'asset_class': asset_class,
        'executive_summary': f"{summary} indicates a material regulatory signal that may affect market participants. The source text suggests a need for operational review and compliance monitoring.",
        'impact_tags': tags,
        'risk_rating': risk,
        'confidence': 0.3,
        'critic_review': {
            'claim_status': 'unknown',
            'summary': 'AI review is not enabled because no model configuration is available.',
            'discrepancies': [],
            'supporting_evidence': [],
            'confidence': 0.0,
        },
    }


def _structured_prompt(prompt: str, schema: type[BaseModel]) -> BaseModel:
    llm = build_llm()
    if llm is None:
        raise ValueError('AI provider is disabled in the environment configuration.')
    return llm.with_structured_output(schema).invoke(prompt)
    # return llm.invoke(prompt)


def analyze_regulatory_text(raw_text: str, title: str = '', jurisdiction: str = 'unknown', asset_class: str = 'unknown', primary_legal_source: str | None = None) -> dict[str, Any]:
    """Convert raw regulatory text into a normalized regulatory analysis JSON object."""
    text = (raw_text or '').strip()
    if not text:
        print('Warning: empty regulatory text provided to analyze_regulatory_text()')
        return _fallback_analysis('', title, jurisdiction, asset_class)
    try:
        prompt = f"""
You are a regulatory analysis assistant. Read the following regulatory text and produce a normalized assessment.

Title: {title or 'Unknown title'}
Jurisdiction: {jurisdiction}
Asset class: {asset_class}

Source text:
{text[:12000]}

Return JSON with:
- executive_summary: 2-4 sentences in plain English
- impact_tags: list of lower-case impact keywords
- risk_rating: one of low, medium, high, critical, unknown
- confidence: float in 0..1
- key_findings: 3 bullet points as short strings
- source_citations: brief references to the cited source text, if any
"""
        result = _structured_prompt(prompt, ExecutiveSummary)
        analysis = result.model_dump()

        critic_prompt = f"""
You are a critic agent. Compare the following secondary claim against the primary legal source.

Secondary claim / summary:
{analysis['executive_summary']}

Primary legal source:
{(primary_legal_source or text)[:12000]}

Return JSON with:
- claim_status: one of supported, unsupported, mixed, unknown
- summary: brief explanation of whether the claim is substantiated
- discrepancies: list of discrepancies or missing support
- supporting_evidence: list of evidence fragments that support the claim
- confidence: float in 0..1
"""
        critic = _structured_prompt(critic_prompt, CriticReview)
        return {
            'jurisdiction': jurisdiction,
            'asset_class': asset_class,
            'executive_summary': analysis['executive_summary'],
            'impact_tags': analysis.get('impact_tags', []),
            'risk_rating': analysis.get('risk_rating', 'unknown'),
            'confidence': float(analysis.get('confidence', 0.0)),
            'critic_review': critic.model_dump(),
        }
    except Exception as e:
        print('Warning: AI analysis failed; falling back to structured summary.', e)
        return _fallback_analysis(text, title, jurisdiction, asset_class)


def cross_check_secondary_claim(secondary_claim: str, primary_legal_source: str, jurisdiction: str = 'unknown') -> dict[str, Any]:
    """Critic agent cross-checking of a secondary news claim against primary legal source."""
    prompt = f"""
You are reviewing a secondary claim against a primary legal source.
Jurisdiction: {jurisdiction}
Secondary claim: {secondary_claim}
Primary legal source:
{primary_legal_source[:12000]}

Return JSON with:
- claim_status: supported / unsupported / mixed / unknown
- summary: brief explanation
- discrepancies: apparent mismatches or unsupported statements
- supporting_evidence: concrete evidence fragments
- confidence: float 0..1
"""
    try:
        return _structured_prompt(prompt, CriticReview).model_dump()
    except Exception:
        return {
            'claim_status': 'unknown',
            'summary': 'Model review could not be completed. Please verify against the primary source manually.',
            'discrepancies': [],
            'supporting_evidence': [],
            'confidence': 0.0,
        }


def get_agent_status() -> dict[str, Any]:
    settings = get_agent_settings()
    provider = settings['provider']
    base_url = settings['ollama_base_url'] if provider == 'ollama' else settings['openai_base_url']
    model = settings['ollama_model'] if provider == 'ollama' else settings['openai_model']
    return {
        'enabled': settings['enabled'],
        'provider': provider,
        'base_url': base_url,
        'model': model,
        'message': 'AI workflow ready' if settings['enabled'] else 'AI workflow disabled; edit .env to enable it.',
    }
