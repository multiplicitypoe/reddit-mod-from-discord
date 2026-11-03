import assert from 'node:assert/strict'
import {test} from 'node:test'
import {type Deps, onCommentDelete, onCommentReport, onPostDelete, onPostReport} from './reports.ts'

const HOOK = 'https://discord.com/api/webhooks/123456/token'

function fakes(opts: {url?: string | undefined; botId?: string; responses?: Array<{status: number; retryAfterS?: number} | Error>} = {}) {
  const kv = new Map<string, string>()
  const sent: unknown[] = []
  const deleted: string[] = []
  const responses = [...(opts.responses ?? [])]
  const sleeps: number[] = []
  let ids = 0
  const deps: Deps = {
    redis: {
      incrBy: async (k, by) => { const v = Number(kv.get(k) ?? 0) + by; kv.set(k, String(v)); return v },
      get: async k => kv.get(k),
      set: async (k, v) => void kv.set(k, v),
      del: async k => void kv.delete(k),
      expire: async () => {},
    },
    webhookUrl: async () => ('url' in opts ? opts.url : HOOK),
    botUserId: async () => opts.botId,
    userName: async id => (id === 't2_u1' ? 'Carol_Example' : null),
    send: async (_url, body) => {
      const next = responses.shift() ?? {status: 200, messageId: `m${++ids}`}
      if (next instanceof Error) throw next
      if (next.status < 300) sent.push(body)
      return next
    },
    deleteMessage: async (_url, id) => { deleted.push(id); return 204 },
    sleep: async ms => void sleeps.push(ms),
  }
  return {deps, sent, kv, sleeps, deleted}
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

test('the alert mentions the configured bot so it can read the embed, and nobody else', async () => {
  const f = fakes({botId: '123456789012345678'})
  await onPostReport(post(), f.deps)
  const body = f.sent[0] as {content?: string; allowed_mentions: unknown}
  assert.equal(body.content, '<@123456789012345678>')
  assert.deepEqual(body.allowed_mentions, {users: ['123456789012345678']})
})


test('when the author deletes a posted item, its message is deleted and the bot is told, with no content', async () => {
  const f = fakes({botId: '123456789012345678'})
  await onPostReport(post(), f.deps)
  assert.equal(await onPostDelete({postId: 't3_p1', subreddit: {name: 'examplesub'}}, f.deps), 'cleared')
  assert.deepEqual(f.deleted, ['m1'])
  const notice = f.sent[1] as {content: string; embeds: Array<{title: string; description?: string; footer: {text: string}}>}
  assert.equal(notice.content, '<@123456789012345678>')
  assert.equal(notice.embeds[0]!.footer.text, 'rmfd:v1:deleted t3_p1 r/examplesub')
  assert.equal(notice.embeds[0]!.description, undefined)
  assert.equal(f.kv.size, 0)
})

test('a deleted item that was never posted is left alone', async () => {
  const f = fakes({botId: '123456789012345678'})
  assert.equal(await onCommentDelete({commentId: 't1_c9', subreddit: {name: 'examplesub'}}, f.deps), 'not-posted')
  assert.equal(f.sent.length, 0)
  assert.equal(f.deleted.length, 0)
})

test('without a bot id, a deleted comment only has its own message deleted', async () => {
  const f = fakes()
  await onCommentReport(comment(), f.deps)
  assert.equal(await onCommentDelete({commentId: 'c1', subreddit: {name: 'examplesub'}}, f.deps), 'cleared')
  assert.deepEqual(f.deleted, ['m1'])
  assert.equal(f.sent.length, 1)
})
