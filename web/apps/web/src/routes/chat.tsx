import { useState, useRef, useEffect, useCallback } from 'react'
import { Send, RotateCcw, ChevronDown } from 'lucide-react'
import { useAuthStore } from '@/stores/auth'
import { loadLLMConfig, loadAllModels, runChatAgentStream, type LLMConfig, type ModelOption } from '@/lib/chat-agent'
import { MarkdownContent } from '@/components/markdown'
import { WyckoffLoading } from '@/components/loading'

const TOOL_LABELS: Record<string, string> = {
  search_stock: '搜索股票',
  view_portfolio: '查看持仓',
  market_overview: '大盘水温',
  query_recommendations: '推荐跟踪',
  query_tail_buy: '尾盘记录',
  update_portfolio: '调仓操作',
  analyze_stock: '个股诊断',
  screen_stocks: '漏斗选股',
  generate_ai_report: 'AI 研报',
  generate_strategy_decision: '策略建议',
}

interface Message {
  role: 'user' | 'assistant'
  content: string
  isError?: boolean
}

export function ChatPage() {
  const user = useAuthStore((s) => s.user)
  const [messages, setMessages] = useState<Message[]>([])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [llmConfig, setLlmConfig] = useState<LLMConfig | null>(null)
  const [models, setModels] = useState<ModelOption[]>([])
  const [showModelPicker, setShowModelPicker] = useState(false)
  const [streamingText, setStreamingText] = useState('')
  const [toolStatus, setToolStatus] = useState('')
  const scrollRef = useRef<HTMLDivElement>(null)
  const abortRef = useRef(false)
  const pickerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (user) {
      loadLLMConfig(user.id).then(setLlmConfig)
      loadAllModels(user.id).then(setModels)
    }
  }, [user])

  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (pickerRef.current && !pickerRef.current.contains(e.target as Node)) {
        setShowModelPicker(false)
      }
    }
    document.addEventListener('mousedown', handleClick)
    return () => document.removeEventListener('mousedown', handleClick)
  }, [])

  const scrollToBottom = useCallback(() => {
    requestAnimationFrame(() => {
      scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' })
    })
  }, [])

  useEffect(() => {
    scrollToBottom()
  }, [messages, streamingText, scrollToBottom])

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!input.trim() || loading) return

    if (!llmConfig) {
      setError('请先在设置页配置 LLM API Key')
      return
    }

    const userMsg: Message = { role: 'user', content: input.trim() }
    const newMessages = [...messages, userMsg]
    setMessages(newMessages)
    setInput('')
    setError('')
    setLoading(true)
    setStreamingText('')
    setToolStatus('')
    abortRef.current = false

    let accumulated = ''

    const chatHistory = newMessages
      .filter((m) => !m.isError)
      .map((m) => ({
        role: m.role as 'user' | 'assistant',
        content: m.content,
      }))

    await runChatAgentStream(
      llmConfig,
      user!.id,
      chatHistory,
      {
        onText: (text) => {
          if (abortRef.current) return
          accumulated = text
          setStreamingText(text)
          setToolStatus('')
        },
        onToolCall: (toolName) => {
          if (abortRef.current) return
          const label = TOOL_LABELS[toolName] || toolName
          setToolStatus(`正在调用：${label}`)
        },
        onFinish: (finalText) => {
          if (abortRef.current) return
          const content = finalText || accumulated
          if (content) {
            setMessages((prev) => [...prev, { role: 'assistant', content }])
          }
          setStreamingText('')
          setToolStatus('')
          setLoading(false)
        },
        onError: (err) => {
          const msg = err.message || '请求失败'
          setError(msg)
          if (accumulated) {
            setMessages((prev) => [...prev, { role: 'assistant', content: accumulated }])
          } else {
            setMessages((prev) => [...prev, { role: 'assistant', content: `⚠️ ${msg}`, isError: true }])
          }
          setStreamingText('')
          setToolStatus('')
          setLoading(false)
        },
      },
    )
  }

  function handleNewChat() {
    abortRef.current = true
    setMessages([])
    setStreamingText('')
    setToolStatus('')
    setError('')
    setLoading(false)
  }

  return (
    <div className="flex h-full flex-col">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-border px-6 py-3">
        <div className="flex items-center gap-3">
          <h1 className="text-lg font-semibold">读盘室</h1>
          {llmConfig && (
            <div className="relative" ref={pickerRef}>
              <button
                onClick={() => setShowModelPicker(!showModelPicker)}
                className="flex items-center gap-1 rounded-full bg-green-50 px-2.5 py-0.5 text-[11px] text-green-700 hover:bg-green-100 transition-colors"
              >
                {llmConfig.model}
                <ChevronDown size={10} />
              </button>
              {showModelPicker && models.length > 0 && (
                <div className="absolute left-0 top-full z-50 mt-1 w-56 rounded-lg border border-border bg-background shadow-lg">
                  {models.map((m) => (
                    <button
                      key={`${m.provider}-${m.model}`}
                      onClick={() => {
                        setLlmConfig({ api_key: m.api_key, model: m.model, base_url: m.base_url })
                        setShowModelPicker(false)
                      }}
                      className={`flex w-full items-center justify-between px-3 py-2 text-left text-xs hover:bg-muted/50 ${
                        m.model === llmConfig.model ? 'bg-muted/30 font-medium' : ''
                      }`}
                    >
                      <span>{m.model}</span>
                      <span className="text-muted-foreground">{m.label}</span>
                    </button>
                  ))}
                </div>
              )}
            </div>
          )}
          {!llmConfig && user && (
            <span className="rounded-full bg-amber-50 px-2 py-0.5 text-[11px] text-amber-700">
              未配置 API Key
            </span>
          )}
        </div>
        <button
          onClick={handleNewChat}
          className="flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-sm text-muted-foreground hover:bg-muted/50"
        >
          <RotateCcw size={14} />
          新对话
        </button>
      </div>

      {/* Messages */}
      <div ref={scrollRef} className="flex-1 overflow-auto px-6 py-4">
        {messages.length === 0 && !streamingText ? (
          <div className="flex h-full flex-col items-center justify-center text-muted-foreground">
            <div className="mb-4 text-4xl">📈</div>
            <p className="text-sm font-medium">我是威科夫，只看供需和主力行为</p>
            <p className="mt-2 text-xs text-muted-foreground">试试问我：</p>
            <div className="mt-3 flex flex-wrap justify-center gap-2">
              {['我有什么持仓', '大盘怎么样', '最近推荐了什么', '帮我搜一下宁德时代', '帮我选股', '给个操作建议'].map((q) => (
                <button
                  key={q}
                  onClick={() => setInput(q)}
                  className="rounded-full border border-border px-3 py-1 text-xs text-muted-foreground hover:bg-muted/50"
                >
                  {q}
                </button>
              ))}
            </div>
          </div>
        ) : (
          <div className="space-y-4">
            {messages.map((msg, i) => (
              <div
                key={i}
                className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}
              >
                <div
                  className={`max-w-[80%] rounded-2xl px-4 py-2.5 text-sm ${
                    msg.role === 'user'
                      ? 'bg-primary text-primary-foreground whitespace-pre-wrap'
                      : msg.isError
                        ? 'bg-red-50 text-red-700 border border-red-200'
                        : 'bg-muted text-foreground'
                  }`}
                >
                  {msg.role === 'user' ? msg.content : <MarkdownContent content={msg.content} />}
                </div>
              </div>
            ))}

            {/* Streaming response */}
            {loading && (
              <div className="flex justify-start">
                <div className="max-w-[80%] rounded-2xl bg-muted px-4 py-2.5 text-sm text-foreground">
                  {streamingText ? (
                    <span><MarkdownContent content={streamingText} className="inline" /><span className="animate-pulse">▌</span></span>
                  ) : toolStatus ? (
                    <span className="text-muted-foreground">
                      <span className="mr-1.5 inline-block h-2 w-2 animate-pulse rounded-full bg-primary" />
                      {toolStatus}
                    </span>
                  ) : (
                    <WyckoffLoading size="sm" />
                  )}
                </div>
              </div>
            )}
          </div>
        )}
      </div>

      {/* Error */}
      {error && (
        <div className="mx-6 mb-2 rounded-lg bg-red-50 px-3 py-2 text-xs text-red-700">{error}</div>
      )}

      {/* Input */}
      <div className="border-t border-border px-6 py-4">
        <form onSubmit={handleSubmit} className="flex items-center gap-2">
          <input
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="输入消息..."
            className="flex-1 rounded-xl border border-border bg-background px-4 py-2.5 text-sm outline-none focus:ring-2 focus:ring-ring/20"
          />
          <button
            type="submit"
            disabled={!input.trim() || loading}
            className="flex h-10 w-10 items-center justify-center rounded-xl bg-primary text-primary-foreground disabled:opacity-40"
          >
            <Send size={16} />
          </button>
        </form>
      </div>
    </div>
  )
}

