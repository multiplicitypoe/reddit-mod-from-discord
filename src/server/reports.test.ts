import assert from 'node:assert/strict'
import {test} from 'node:test'
import {type Deps, onCommentReport, onPostReport} from './reports.ts'

const HOOK = 'https://discord.com/api/webhooks/123456/token'

function fakes(opts: {url?: string | undefined; responses?: Array<{status: number; retryAfterS?: number} | Error>} = {}) {
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
  assert.equal(f.kv.size, 0)
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
  assert.equal(f.kv.size, 0)
  assert.equal(await onPostReport({reason: 'x'}, f.deps), 'ignored')
})
