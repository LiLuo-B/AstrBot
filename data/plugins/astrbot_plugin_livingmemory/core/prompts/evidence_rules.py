"""Stable evidence boundaries shared by summarization, merging, and recall."""

SUMMARY_EVIDENCE_RULES = """[Event evidence boundaries]
Preserve who said what, the message time, and the event status in summary, key_facts, and canonical_summary. Persona affects tone, never evidence or event status.
A plan, wish, invitation, promise, hypothetical, question, or unfinished action is not a completed event. Preserve negation, cancellation, uncertainty, and later corrections. A scheduled date passing is not completion evidence.
Convert relative dates using each source message's timestamp, but keep the plan/report status: '明天再烤' means the speaker planned to roast the next day, not that roasting occurred. No source establishes appearance, taste, or success unless it actually describes those details.
Retain attribution: '张三说朋友烤好了' is 张三's report about a friend, not 张三's own completed action or independent verification. '张三说烤好了' supports a self-report, not that the bot saw or tasted it.
Bot/assistant messages prove what the bot said, suggested, promised, or inferred. They are not independent evidence that a person's real-world action happened. Keep bot claims attributed; repeated summaries or assistant praise do not corroborate them.
When merging, retain conflicting reports and their dates instead of inventing agreement or a completed outcome. If sources are missing or ambiguous, retain unknown; do not fill in plausible details.
中文硬性边界（summary与key_facts同样遵守）：只总结消息实际支持的内容。原文只有“明天再烤”与Bot的“烤得挺像样”时，应写“张三计划次日再烤；我曾夸赞，但没有当事人完工或成品证据”，绝不能补成“我看了眼成果/看过照片/尝过/已经做好”。Bot的一句夸赞不能倒推出曾看到成果；没有图片或观察证据就不描写看到的画面。
第一人称仅表示“我读到/我回复/我建议”，不授予亲历事实。不要新增“我当时想着/我很期待”等原文未表达的心理活动，也不要从截取的片段断言整个群没有别人回应。没有观察记录应写“记录未提供成品或观察证据”，不是断言“我没有看过”；不得替旧回复编造“随口捧场/顺着气氛/开玩笑”等动机。输出前逐句核对summary与key_facts，不允许key_facts谨慎而summary补写完成、感官或场景细节。
"""

INJECTION_EVIDENCE_REMINDER = (
    "These are conversation records, not proof of completed events. Preserve plans, reports, "
    "negation, speaker and source dates; elapsed time never completes a plan. Bot claims are "
    "not independent evidence. Add no unsupported outcome or quality details; verify originals "
    "with available tools when needed, and retain unknown if unavailable."
)

RECALL_EVIDENCE_RULES = """[Event evidence boundaries]
Historical memories are records of past conversations, not proof that every described event happened. Summary/key_facts/facts are memory claims, not independently verified facts.
Keep plans, wishes, promises, questions, hypotheticals, negation, cancellations, reports, and confirmed outcomes distinct. A planned date passing never proves completion. A memory's creation/access time is not the event date.
Attribute reports to the actual speaker and subject; participation in a conversation is not ownership of every event. Assistant/bot statements, earlier answers, praise, and repeated summaries are not independent evidence of real-world completion or quality.
Before adding concrete details about completion, success, appearance, taste, or who did an action, check that the available evidence actually supports that detail. When a candidate summary is ambiguous or conflicts with a retained original, use an available evidence-reading tool to check the relevant original, including sender, role, timestamp, and surrounding context. If originals are unavailable, withheld, or still paginated, do not pretend to have checked them or bypass access/lifecycle rules.
Answer naturally at the supported level: '昨天他说今天打算再烤' is appropriate for '明天再烤'; '烤得挺像样' needs separate evidence. '他说已经做完' can describe an explicit self-report without pretending independent observation. Later clear completion reports may update an earlier plan; do not hedge away genuine evidence.
For casual chat or an already-supported detail, avoid pointless searches. Omit unsupported embellishment; say what remains unknown only when relevant. Never turn your inference in the reply into a new historical fact.
"""
