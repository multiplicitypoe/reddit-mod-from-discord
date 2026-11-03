import {context, reddit as realReddit, redis as realRedis, settings} from '@devvit/web/server'
import {type Reported, buildAlert, buildDeletedNotice, isDiscordWebhook} from '../shared/alert.ts'

export type Deps = {
  redis: {
    incrBy(key: string, value: number): Promise<number>
    get(key: string): Promise<string | undefined>
    set(key: string, value: string): Promise<unknown>
    del(key: string): Promise<unknown>
    expire(key: string, seconds: number): Promise<unknown>
  }
  webhookUrl(): Promise<string | undefined>
  botUserId(): Promise<string | undefined>
  userName(userId: string): Promise<string | null>
  send(url: string, body: unknown): Promise<{status: number; retryAfterS?: number; messageId?: string}>
  deleteMessage(url: string, messageId: string): Promise<number>
  sleep(ms: number): Promise<void>
}

const ALERT_TTL_S = 30 * 24 * 60 * 60
const alertKey = (fullname: string) => `alerted:${fullname}`
// The webhook message posted for an item, so it can be deleted if the author deletes the item.
const messageKey = (fullname: string) => `message:${fullname}`

const realDeps: Deps = {
  redis: realRedis as unknown as Deps['redis'],
  webhookUrl: () => settings.get<string>('discordWebhook'),
  botUserId: () => settings.get<string>('discordBotUserId'),
  async userName(userId) {
    try {
      return (await realReddit.getUserById(userId as `t2_${string}`))?.username ?? null
    } catch {
      return null
    }
  },
  async send(url, body) {
    // wait=true makes Discord return the message, whose id is kept for deleting it later.
    const res = await fetch(`${url}?wait=true`, {
      method: 'POST',
      headers: {'content-type': 'application/json'},
      body: JSON.stringify(body),
    })
    const data = (await res.json().catch(() => ({}))) as {retry_after?: number; id?: string}
    return {
      status: res.status,
      retryAfterS: res.status === 429 ? data.retry_after : undefined,
      messageId: res.ok && typeof data.id === 'string' ? data.id : undefined,
    }
  },
  async deleteMessage(url, messageId) {
    const res = await fetch(`${url}/messages/${messageId}`, {method: 'DELETE'})
    return res.status
  },
  sleep: ms => new Promise(r => setTimeout(r, ms)),
}

// One alert per reported item. Returns what happened, for the log.
export async function alertOnce(r: Reported, deps: Deps = realDeps): Promise<'sent' | 'duplicate' | 'no-webhook' | 'failed'> {
  const url = await deps.webhookUrl()
  if (!url || !isDiscordWebhook(url)) return 'no-webhook'

  // incrBy is atomic: whoever gets 1 owns this item's alert. The key is released if sending fails.
  const key = alertKey(r.fullname)
  if ((await deps.redis.incrBy(key, 1)) !== 1) return 'duplicate'
  await deps.redis.expire(key, ALERT_TTL_S)

  const body = buildAlert({...r, author: r.author}, await deps.botUserId())
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const res = await deps.send(url, body)
      if (res.status >= 200 && res.status < 300) {
        if (res.messageId) {
          await deps.redis.set(messageKey(r.fullname), res.messageId)
          await deps.redis.expire(messageKey(r.fullname), ALERT_TTL_S)
        }
        return 'sent'
      }
      if (res.status === 429 && res.retryAfterS !== undefined && res.retryAfterS <= 10) {
        await deps.sleep(res.retryAfterS * 1000)
        continue
      }
      break
    } catch {
      // fall through to release the key
    }
  }
  await deps.redis.del(key)
  return 'failed'
}

type PostEvent = {
  post?: {id: string; title: string; selftext: string; authorId: string; numReports: number; createdAt: number; permalink: string}
  subreddit?: {name: string}
  reason: string
}
type CommentEvent = {
  comment?: {id: string; body: string; author: string; numReports: number; createdAt: number; permalink: string}
  subreddit?: {name: string}
  reason: string
}

export async function onPostReport(event: PostEvent, deps: Deps = realDeps) {
  const p = event.post
  if (!p) return 'ignored'
  return alertOnce(
    {
      fullname: p.id.startsWith('t3_') ? p.id : `t3_${p.id}`,
      kind: 'post',
      subreddit: event.subreddit?.name ?? context.subredditName,
      author: await deps.userName(p.authorId),
      permalink: p.permalink,
      title: p.title,
      text: p.selftext,
      reportReason: event.reason,
      totalReports: p.numReports,
      createdAtMs: p.createdAt,
    },
    deps,
  )
}

export async function onCommentReport(event: CommentEvent, deps: Deps = realDeps) {
  const c = event.comment
  if (!c) return 'ignored'
  const author = c.author.startsWith('t2_') ? await deps.userName(c.author) : c.author
  return alertOnce(
    {
      fullname: c.id.startsWith('t1_') ? c.id : `t1_${c.id}`,
      kind: 'comment',
      subreddit: event.subreddit?.name ?? context.subredditName,
      author,
      permalink: c.permalink,
      title: null,
      text: c.body,
      reportReason: event.reason,
      totalReports: c.numReports,
      createdAtMs: c.createdAt,
    },
    deps,
  )
}

// When the author deletes an item that was posted, its webhook message is deleted, and the bot is told
// to clear the copy it made. Items that were never posted are left alone.
export async function clearDeleted(
  fullname: string,
  subreddit: string,
  deps: Deps = realDeps,
): Promise<'cleared' | 'not-posted' | 'no-webhook'> {
  const url = await deps.webhookUrl()
  if (!url || !isDiscordWebhook(url)) return 'no-webhook'
  const messageId = await deps.redis.get(messageKey(fullname))
  const posted = messageId !== undefined || (await deps.redis.get(alertKey(fullname))) !== undefined
  if (!posted) return 'not-posted'

  if (messageId) {
    try {
      await deps.deleteMessage(url, messageId)
    } catch {
      // The bot usually deleted it already.
    }
  }
  const botUserId = await deps.botUserId()
  if (botUserId) {
    try {
      await deps.send(url, buildDeletedNotice(fullname, subreddit, botUserId))
    } catch {
      // Nothing else to clear without the bot.
    }
  }
  await deps.redis.del(messageKey(fullname))
  await deps.redis.del(alertKey(fullname))
  return 'cleared'
}

type DeleteEvent = {postId?: string; commentId?: string; subreddit?: {name: string}}

export async function onPostDelete(event: DeleteEvent, deps: Deps = realDeps) {
  if (!event.postId) return 'ignored'
  const id = event.postId.startsWith('t3_') ? event.postId : `t3_${event.postId}`
  return clearDeleted(id, event.subreddit?.name ?? context.subredditName, deps)
}

export async function onCommentDelete(event: DeleteEvent, deps: Deps = realDeps) {
  if (!event.commentId) return 'ignored'
  const id = event.commentId.startsWith('t1_') ? event.commentId : `t1_${event.commentId}`
  return clearDeleted(id, event.subreddit?.name ?? context.subredditName, deps)
}
