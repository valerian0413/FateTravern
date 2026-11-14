from app.modules.intent import recognize


async def test_discussion_question():
    intent = await recognize(None, None, {}, '我能用自然知识判断风向吗', '交流')
    assert intent.kind.value == 'discussion'


async def test_explore_action():
    intent = await recognize(None, None, {}, '我看看有哪些飞艇', '行动')
    assert intent.kind.value == 'explore'
    assert intent.normalized_action


async def test_cocreate():
    intent = await recognize(None, None, {}, '我想这里有一位认识我父亲的船长', '共创')
    assert intent.kind.value == 'cocreate'


async def test_ambiguous_clarify():
    intent = await recognize(None, None, {}, '嗯', '交流')
    assert intent.kind.value in ('clarify', 'discussion')
