"""Knowledge integrity audit: does the stored chain still hold together?

Phase 3.12 (§61). The knowledge base enforces a lot at write time --
foreign keys, unique identities, append-only evidence. This service asks
the remaining questions that a constraint cannot: is the *chain* intact?

    Paper -> Mention -> Entity -> Reaction -> ReactionRecord -> Evidence
    Route -> RouteStep -> Reaction

A reaction whose record is missing cannot answer "what experiment is this
based on" (§28). A reaction marked `EXPERIMENTALLY_SUPPORTED` whose
citations do not resolve is a claim without its evidence (§77). A mention
with no scope is a "compound 7" waiting to collide with another paper
(§59). Each of these is reported as an issue with the objects involved --
never repaired automatically. An audit that quietly "fixes" knowledge is
the same failure as a model inventing it (§6): what is broken is said,
not rewritten.

Read-only over the knowledge base (§108). No LLM, no network, no SQL
outside the repository.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from crea.core.provenance import utc_now_iso


class IntegrityIssue(BaseModel):
    """One place where the chain does not hold.

    `code` is a stable machine-readable name; `detail` is a sentence for a
    person; `object_ids` names what is involved so a caller can look.
    """

    code: str
    detail: str
    object_ids: list[str] = Field(default_factory=list)


class KnowledgeIntegrityReport(BaseModel):
    """What the audit found. `ok` is True only with zero issues."""

    issues: list[IntegrityIssue] = Field(default_factory=list)
    checked: dict[str, int] = Field(default_factory=dict)
    generated_at: str = Field(default_factory=utc_now_iso)

    @property
    def ok(self) -> bool:
        return not self.issues


class KnowledgeIntegrityService:
    """Audits the knowledge chain. Changes nothing."""

    def audit(self, kb) -> KnowledgeIntegrityReport:
        issues: list[IntegrityIssue] = []
        checked: dict[str, int] = {}

        entities = kb.list_entities()
        reactions = kb.list_reactions()
        records = kb.list_reaction_records()
        mentions = kb.list_mentions()
        evidence = kb.list_evidence()
        sessions = kb.list_sessions()
        checked.update({
            "entities": len(entities),
            "reactions": len(reactions),
            "reaction_records": len(records),
            "mentions": len(mentions),
            "evidence": len(evidence),
            "sessions": len(sessions),
        })

        evidence_ids = {e.evidence_id for e in evidence}
        record_ids = {r.reaction_id for r in records}
        entity_ids = {e.entity_id for e in entities}
        reaction_by_id = {r.reaction_id: r for r in reactions}

        # -- ReactionRecord <-> Reaction (§25/§28/§60): the chain a reader
        #    needs to answer "which experiment is this reaction based on".
        #    The link is the stored row's (record_id -> reaction_id) pair.
        links = kb.store.reaction_record_links()
        linked_records = sum(1 for _r, rid in links if rid in reaction_by_id)
        linked_record_ids = {r for r, _rid in links}
        for record_id, reaction_id in links:
            if reaction_id not in reaction_by_id:
                issues.append(IntegrityIssue(
                    code="ORPHAN_REACTION_RECORD",
                    detail=(f"记录 {record_id} 指向不存在的反应 {reaction_id}"),
                    object_ids=[record_id, reaction_id]))
        for reaction in reactions:
            if not reaction.record_id:
                issues.append(IntegrityIssue(
                    code="REACTION_WITHOUT_RECORD",
                    detail=(f"reaction {reaction.reaction_id} 没有实验记录链路"
                            f"（record_id 为空）"),
                    object_ids=[reaction.reaction_id]))
            elif reaction.record_id not in record_ids:
                issues.append(IntegrityIssue(
                    code="REACTION_RECORD_MISSING",
                    detail=(f"reaction {reaction.reaction_id} 指向记录 "
                            f"{reaction.record_id}，但记录不在知识库中"),
                    object_ids=[reaction.reaction_id, reaction.record_id]))
        for record in records:
            if record.reaction_id not in linked_record_ids:
                issues.append(IntegrityIssue(
                    code="RECORD_WITHOUT_REACTION",
                    detail=(f"记录 {record.reaction_id} 没有链接到任何反应"),
                    object_ids=[record.reaction_id]))

        # -- status honesty (§77/§49): a supported status must have its
        #    evidence actually stored.
        for reaction in reactions:
            stored_refs = [e.evidence_id
                           for e in kb.get_evidence_for_reaction(
                               reaction.reaction_id)]
            if reaction.status.value in ("evidence_backed",
                                         "experimentally_supported") \
                    and not stored_refs:
                issues.append(IntegrityIssue(
                    code="SUPPORTED_STATUS_WITHOUT_EVIDENCE",
                    detail=(f"reaction {reaction.reaction_id} 状态为 "
                            f"{reaction.status.value}，但没有任何可解析证据"),
                    object_ids=[reaction.reaction_id]))
            for ref in reaction.evidence_refs:
                if ref not in evidence_ids:
                    issues.append(IntegrityIssue(
                        code="DANGLING_EVIDENCE_REF",
                        detail=(f"reaction {reaction.reaction_id} 引用证据 "
                                f"{ref}，但该证据不在知识库中"),
                        object_ids=[reaction.reaction_id, ref]))

        # -- mention scope (§20/§59) and entity links
        for mention in mentions:
            if not mention.source_id:
                issues.append(IntegrityIssue(
                    code="MENTION_WITHOUT_SCOPE",
                    detail=(f"mention {mention.mention_id} "
                            f"({mention.raw_text!r}) 没有 paper/document scope"),
                    object_ids=[mention.mention_id]))
            if mention.entity_id and mention.entity_id not in entity_ids:
                issues.append(IntegrityIssue(
                    code="MENTION_DANGLING_ENTITY",
                    detail=(f"mention {mention.mention_id} 指向不存在的实体 "
                            f"{mention.entity_id}"),
                    object_ids=[mention.mention_id, mention.entity_id]))

        # -- provenance (§47/§60): a fact with no provenance cannot say
        #    where it came from.
        for entity in entities:
            if entity.provenance is None:
                issues.append(IntegrityIssue(
                    code="ENTITY_WITHOUT_PROVENANCE",
                    detail=f"entity {entity.entity_id} 没有 provenance",
                    object_ids=[entity.entity_id]))
        for reaction in reactions:
            if reaction.provenance is None:
                issues.append(IntegrityIssue(
                    code="REACTION_WITHOUT_PROVENANCE",
                    detail=f"reaction {reaction.reaction_id} 没有 provenance",
                    object_ids=[reaction.reaction_id]))
        for item in evidence:
            if item.provenance is None:
                issues.append(IntegrityIssue(
                    code="EVIDENCE_WITHOUT_PROVENANCE",
                    detail=f"evidence {item.evidence_id} 没有 provenance",
                    object_ids=[item.evidence_id]))

        # -- session links (§25): the session must point at stored objects.
        for session in sessions:
            for label, ids in (("entity", session.chemical_entity_ids),
                               ("reaction", session.reaction_ids),
                               ("evidence", session.evidence_ids)):
                for object_id in ids:
                    pool = {"entity": entity_ids, "reaction":
                            set(reaction_by_id), "evidence": evidence_ids}[label]
                    if object_id not in pool:
                        issues.append(IntegrityIssue(
                            code="SESSION_LINK_DANGLING",
                            detail=(f"session {session.session_id} 引用"
                                    f"{label} {object_id}，但对象不存在"),
                            object_ids=[session.session_id, object_id]))

        checked["linked_records"] = linked_records
        return KnowledgeIntegrityReport(issues=issues, checked=checked)


__all__ = [
    "IntegrityIssue",
    "KnowledgeIntegrityReport",
    "KnowledgeIntegrityService",
]
