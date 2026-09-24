"""兼容旧导入路径; 实现位于 domain.outline."""

from shader_deep.domain.outline import (
    IssueProposal as IssueProposal,
    IssueVerification as IssueVerification,
    OutlineReview as OutlineReview,
    VerifyOutlineReview as VerifyOutlineReview,
    require_issue_ids as require_issue_ids,
    review_error as review_error,
    revised_outline as revised_outline,
)
