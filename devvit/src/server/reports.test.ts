import assert from 'node:assert/strict'
import {test} from 'node:test'
import {type Deps, onCommentReport, onPostReport} from './reports.ts'

const HOOK = 'https://discord.com/api/webhooks/123456/token'

function fakes(opts: {url?: string | undefined; botId?: string; responses?: Array<{status: number; retryAfterS?: number} | Error>; thresholds?: {post: number; comment: number}; current?: Map<string, number>} = {}) {
  const kv = new Map<string, number>()
  const sent: unknown[] = []
  const responses = [...(opts.responses ?? [])]
  const sleeps: number[] = []
  const deps: Deps = {
    redis: {
      incrBy: async (k, by) => { const v = (kv.get(k) ?? 0) + by; kv.set(k, v); return v },
      del: async k => void kv.delete(k),
      expire: async () => {},
    },
    webhookUrl: async () => ('url' in opts ? opts.url : HOOK),
    thresholds: async () => opts.thresholds ?? {post: 1, comment: 1},
    currentReports: async fullname => opts.current?.get(fullname) ?? null,
    botUserId: async () => opts.botId,
    userName: async id => (id === 't2_u1' ? 'Carol_Example' : null),
    send: async (_url, body) => {
      const next = responses.shift() ?? {status: 204}
      if (next instanceof Error) throw next
      if (next.status < 300) sent.push(body)
      return next
    },
    sleep: async ms => void sleeps.push(ms),
  }
  return {deps, sent, kv, sleeps}
}

const post = (id = 't3_p1') => ({
  post: {id, title: 'A title', selftext: 'body', authorId: 't2_u1', numReports: 1, createdAt: 1_000, permalink: '/r/examplesub/comments/p1/a/'},
  subreddit: {name: 'examplesub'},
  reason: 'Spam',
})
const comment = (id = 't1_c1') => ({
  comment: {id, body: 'hi', author: 't2_u1', numReports: 1, createdAt: 1_000, permalink: '/r/examplesub/comments/p1/a/c1/'},
  subreddit: {name: 'examplesub'},
  reason: 'Rule 1',
})

test('a reported post sends one alert, and more reports on the same item do not repeat it', async () => {
  const f = fakes()
  assert.equal(await onPostReport(post(), f.deps), 'sent')
  assert.equal(await onPostReport(post(), f.deps), 'duplicate')
  assert.equal(f.sent.length, 1)
})

test('a reported comment resolves the author name and sends', async () => {
  const f = fakes()
  assert.equal(await onCommentReport(comment(), f.deps), 'sent')
  const embed = (f.sent[0] as {embeds: Array<{fields: Array<{value: string}>}>}).embeds[0]!
  assert.equal(embed.fields[0]!.value, 'u/Carol_Example')
})

test('no webhook, or one that is not Discord, sends nothing and does not use up the item', async () => {
  const none = fakes({url: undefined})
  assert.equal(await onPostReport(post(), none.deps), 'no-webhook')
  const bad = fakes({url: 'https://example.com/hook'})
  assert.equal(await onPostReport(post(), bad.deps), 'no-webhook')
  assert.equal(bad.kv.size, 0)
})

test('a failed send releases the item so a later report can alert again', async () => {
  const f = fakes({responses: [{status: 500}]})
  assert.equal(await onPostReport(post(), f.deps), 'failed')
  assert.equal(f.kv.has('alerted:t3_p1'), false)
  assert.equal(await onPostReport(post(), f.deps), 'sent')
})

test('a rate limit is waited out once, and a long one gives up', async () => {
  const f = fakes({responses: [{status: 429, retryAfterS: 2}, {status: 204}]})
  assert.equal(await onPostReport(post(), f.deps), 'sent')
  assert.deepEqual(f.sleeps, [2000])
  const g = fakes({responses: [{status: 429, retryAfterS: 600}]})
  assert.equal(await onPostReport(post('t3_p2'), g.deps), 'failed')
})

test('a network error releases the item and an empty event is ignored', async () => {
  const f = fakes({responses: [new Error('network'), new Error('network')]})
  assert.equal(await onPostReport(post(), f.deps), 'failed')
  assert.equal(f.kv.has('alerted:t3_p1'), false)
  assert.equal(await onPostReport({reason: 'x'}, f.deps), 'ignored')
})

test('the alert mentions the configured bot so it can read the embed, and nobody else', async () => {
  const f = fakes({botId: '123456789012345678'})
  await onPostReport(post(), f.deps)
  const body = f.sent[0] as {content?: string; allowed_mentions: unknown}
  assert.equal(body.content, '<@123456789012345678>')
  assert.deepEqual(body.allowed_mentions, {users: ['123456789012345678']})
})

test('nothing is sent until Reddit counts enough reports', async () => {
  const current = new Map([['t3_p1', 1]])
  const {deps, sent} = fakes({thresholds: {post: 3, comment: 5}, current})
  assert.equal(await onPostReport(post(), deps), 'below-threshold')
  current.set('t3_p1', 2)
  assert.equal(await onPostReport(post(), deps), 'below-threshold')
  current.set('t3_p1', 3)
  assert.equal(await onPostReport(post(), deps), 'sent')
  assert.equal(await onPostReport(post(), deps), 'duplicate')
  assert.equal(sent.length, 1)
})

test('repeat triggers for the same reports are not counted as new reports', async () => {
  const {deps, sent} = fakes({thresholds: {post: 3, comment: 5}, current: new Map([['t3_p1', 2]])})
  for (let i = 0; i < 4; i++) assert.equal(await onPostReport(post(), deps), 'below-threshold')
  assert.equal(sent.length, 0)
})

test("Reddit's own report count counts toward the threshold", async () => {
  const {deps, sent} = fakes({thresholds: {post: 3, comment: 5}})
  const p = post()
  p.post.numReports = 3
  assert.equal(await onPostReport(p, deps), 'sent')
  const c = comment()
  c.comment.numReports = 4
  assert.equal(await onCommentReport(c, deps), 'below-threshold')
  assert.equal(sent.length, 1)
})
