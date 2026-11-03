import assert from 'node:assert/strict'
import {test} from 'node:test'
import {buildAlert, footerFor, isDiscordWebhook, parseFooter, quote} from './alert.ts'

const base = {
  fullname: 't3_abc123', kind: 'post' as const, subreddit: 'examplesub', author: 'Carol_Example',
  permalink: '/r/examplesub/comments/abc123/title/', title: 'Nobody gave me a color', text: 'Body',
  reportReason: 'Rule 3', totalReports: 2, createdAtMs: Date.parse('2026-11-05T18:00:00Z'),
}

test('a post alert has the title, author, count, reason and a footer the bot can parse', () => {
  const a = buildAlert(base)
  const e = a.embeds[0]!
  assert.equal(e.title, 'Reported post: Nobody gave me a color')
  assert.equal(e.url, 'https://www.reddit.com/r/examplesub/comments/abc123/title/')
  assert.deepEqual(e.fields.map(f => f.value), ['u/Carol_Example', '2', 'Rule 3'])
  assert.deepEqual(parseFooter(e.footer.text), {fullname: 't3_abc123', subreddit: 'examplesub'})
  assert.deepEqual(a.allowed_mentions, {parse: []})
})

test('a comment alert is labelled as a comment, and unknown authors and empty bodies are handled', () => {
  const a = buildAlert({...base, kind: 'comment', fullname: 't1_zzz', title: null, author: null, text: '  ', totalReports: 0, reportReason: ''})
  const e = a.embeds[0]!
  assert.equal(e.title, 'Reported comment')
  assert.equal(e.description, '*(no text)*')
  assert.deepEqual(e.fields.map(f => f.value), ['unknown', '1', 'no reason given'])
})

test('long text and titles are clipped, and mentions are defused', () => {
  const a = buildAlert({...base, title: 'T'.repeat(400), text: `@everyone ${'x'.repeat(2000)}`})
  const e = a.embeds[0]!
  assert.ok(e.title.length <= 256)
  assert.ok(e.description.length <= 1010)
  assert.ok(!e.description.includes('@everyone'))
  assert.equal(quote('a\nb', 10), '> a\n> b')
})

test('footer round trip rejects anything else', () => {
  assert.deepEqual(parseFooter(footerFor('t1_x9', 'ExampleSub2')), {fullname: 't1_x9', subreddit: 'ExampleSub2'})
  assert.equal(parseFooter('something else'), null)
})

test('only real Discord webhook URLs are accepted', () => {
  assert.equal(isDiscordWebhook('https://discord.com/api/webhooks/123456/abc-DEF_9'), true)
  assert.equal(isDiscordWebhook('https://evil.example/api/webhooks/123456/abc'), false)
  assert.equal(isDiscordWebhook('http://discord.com/api/webhooks/123456/abc'), false)
  assert.equal(isDiscordWebhook('https://discord.com/api/webhooks/123456/abc?x=1'), false)
})

test('no bot id, or one that is not an id, means no mention at all', () => {
  assert.equal('content' in buildAlert(base), false)
  assert.equal('content' in buildAlert(base, '@everyone'), false)
  assert.deepEqual(buildAlert(base, '@everyone').allowed_mentions, {parse: []})
})
