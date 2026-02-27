
'use client';

import { useState, useRef, useEffect } from 'react';
import Image from 'next/image';

interface Attachment {
  file: File;
  preview: string;
  type: 'image' | 'pdf' | 'text';
}

interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
  attachments?: Attachment[];
}

interface ChatWindowProps {
  onPRDGenerated: (prd: string, attachments: File[], projectKey?: string, epicKey?: string, storyId?: string) => void;
  selectedEpic?: any;
  selectedSpace?: any;
  selectedSkills?: any[];
}

export default function ChatWindow({ onPRDGenerated, selectedEpic, selectedSpace, selectedSkills }: ChatWindowProps) {
  const [input, setInput] = useState('');
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [isGenerating, setIsGenerating] = useState(false);
  const [history, setHistory] = useState<ChatMessage[]>([]);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const historyEndRef = useRef<HTMLDivElement>(null);

  const scrollToBottom = () => {
    historyEndRef.current?.scrollIntoView({ behavior: "smooth" });
  };

  useEffect(() => {
    scrollToBottom();
  }, [history]);

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (!files) return;

    Array.from(files).forEach(file => {
      const isImage = file.type.startsWith('image/');
      const isPDF = file.type === 'application/pdf';
      const isText = file.type === 'text/plain' || file.name.endsWith('.txt') || file.name.endsWith('.log');

      if (isImage || isPDF || isText) {
        if (isImage) {
          const reader = new FileReader();
          reader.onloadend = () => {
            setAttachments(prev => [...prev, { file, preview: reader.result as string, type: 'image' }]);
          };
          reader.readAsDataURL(file);
        } else {
          setAttachments(prev => [...prev, { file, preview: '', type: isPDF ? 'pdf' : 'text' }]);
        }
      }
    });
  };

  const removeAttachment = (index: number) => {
    setAttachments(prev => prev.filter((_, i) => i !== index));
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!input.trim() && attachments.length === 0) return;

    const userMessage: ChatMessage = {
        role: 'user',
        content: input,
        attachments: [...attachments]
    };
    
    setHistory(prev => [...prev, userMessage]);
    setIsGenerating(true);
    
    try {
      const formData = new FormData();
      formData.append('prompt', input);
      attachments.forEach(att => {
        formData.append('attachments', att.file);
      });
      
      if (selectedEpic) {
          const projectKey = selectedEpic.key.split('-')[0];
          formData.append('project_key', projectKey);
          formData.append('epic_key', selectedEpic.key);
      }

      const token = localStorage.getItem('authToken');
      const backendUrl = process.env.NEXT_PUBLIC_BACKEND_URL || process.env.BACKEND_URL || 'http://localhost:8000';
      
      // Debug: Log the backend URL being used
      console.log('[ChatWindow] NEXT_PUBLIC_BACKEND_URL:', process.env.NEXT_PUBLIC_BACKEND_URL);
      console.log('[ChatWindow] BACKEND_URL:', process.env.BACKEND_URL);
      console.log('[ChatWindow] Using backend URL:', backendUrl);
      
      const response = await fetch(`${backendUrl}/autonomous-dev/generate-prd`, {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${token}`
        },
        body: formData
      });

      if (response.ok) {
        const data = await response.json();
        
        const assistantMessage: ChatMessage = {
            role: 'assistant',
            content: `PRD generated and JIRA story created successfully: **${data.story_key}**. Starting autonomous development action...`
        };
        setHistory(prev => [...prev, assistantMessage]);
        
        onPRDGenerated(data.prd, attachments.map(a => a.file), data.project_key, data.epic_key, data.story_id);
        setInput('');
        setAttachments([]);
      } else {
        let errorDetail = "Failed to generate PRD.";
        
        try {
          const data = await response.json();
          errorDetail = data.detail || errorDetail;
        } catch (e) {
          // Fallback if response is not JSON
        }

        let displayMessage = `${errorDetail} Please try again.`;
        
        if (response.status === 401 || errorDetail.toLowerCase().includes("token has expired")) {
          displayMessage = "Your session has expired. Please log out and log in again to continue.";
        }

        const errorMsg: ChatMessage = {
            role: 'assistant',
            content: displayMessage
        };
        setHistory(prev => [...prev, errorMsg]);
      }
    } catch (error) {
      console.error('Error submitting chat:', error);
    } finally {
      setIsGenerating(false);
    }
  };

  return (
    <div className="flex flex-col h-full bg-slate-900 border border-slate-700 rounded-2xl shadow-sm overflow-hidden">
      <div className="p-3 border-b border-slate-700 bg-slate-800 flex justify-between items-center shrink-0">
        <h3 className="font-semibold text-slate-200 flex items-center text-sm">
          <svg className="w-4 h-4 mr-2 text-blue-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z" />
          </svg>
          Product Idea Chat
        </h3>
        {selectedEpic && (
            <div className="flex flex-col items-end">
                <div className="flex items-center gap-2">
                    <span className="text-[10px] text-slate-400 font-medium">Target Space/Epic:</span>
                    <span className="bg-blue-50 text-blue-700 text-[10px] font-bold px-2 py-0.5 rounded border border-blue-100">
                        {selectedSpace?.key || 'Space'}
                    </span>
                    <span className="text-slate-300">/</span>
                    <span className="bg-purple-50 text-purple-700 text-[10px] font-bold px-2 py-0.5 rounded border border-purple-100 uppercase tracking-wider">
                        {selectedEpic.key}
                    </span>
                </div>
            </div>
        )}
      </div>

      <div className="flex-1 flex flex-col min-h-0 overflow-hidden">
        {/* Initial Instructions - Stay Fixed */}
        <div className="p-4 space-y-4 shrink-0 border-b border-slate-700 bg-slate-900">
            {selectedSkills && selectedSkills.length === 0 ? (
                <div className="bg-purple-900/30 text-purple-300 p-3 rounded-lg text-xs border border-purple-800 shadow-sm flex items-start gap-3 animate-pulse">
                    <svg className="w-5 h-5 mt-0.5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                    </svg>
                    <span>Please select <strong>Agent Skill(s)</strong> from the header to provide architecture and coding standards context for your project.</span>
                </div>
            ) : (
                <div className="bg-purple-900/30 text-purple-300 p-3 rounded-lg text-xs border border-purple-800 shadow-sm flex items-start gap-3">
                    <svg className="w-5 h-5 mt-0.5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
                    </svg>
                    <div>
                        <p className="font-semibold text-purple-200">Skill Context Active:</p>
                        {selectedSkills?.map(skill => (
                            <p key={skill.name} className="mt-1 opacity-90">Using <strong>{skill.name}</strong> for standards.</p>
                        ))}
                    </div>
                </div>
            )}
            {!selectedEpic ? (
                <div className="bg-amber-900/30 text-amber-300 p-3 rounded-lg text-xs border border-amber-800 shadow-sm flex items-start gap-3 animate-pulse">
                    <svg className="w-5 h-5 mt-0.5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
                    </svg>
                    <span>Please select a <strong>JIRA Space/Epic</strong> from the header before entering your product idea.</span>
                </div>
            ) : (
                <div className="bg-emerald-900/30 text-emerald-300 p-3 rounded-lg text-xs border border-emerald-800 shadow-sm flex items-start gap-3">
                    <svg className="w-5 h-5 mt-0.5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
                    </svg>
                    <div>
                        <p className="font-semibold text-emerald-200">Contextual Target Locked:</p>
                        <p className="mt-1 opacity-90">Ready to create stories for <strong>{selectedSpace?.name || selectedSpace?.key} / {selectedEpic.fields?.summary || selectedEpic.key}</strong>. If you want to switch, click <strong>JIRA Space/Epic</strong> above.</p>
                    </div>
                </div>
            )}
            <div className="bg-blue-900/30 text-blue-300 p-3 rounded-lg text-xs border border-blue-800 shadow-sm flex items-start gap-3">
                <svg className="w-5 h-5 mt-0.5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
                <span>Tell me about your <strong>product idea</strong> or describe issues to fix. You can attach UI designs (images), requirement docs (PDFs), or logs (*.txt, *.log) for troubleshooting.</span>
            </div>
        </div>

        {/* Chat History - Scrollable */}
        <div className="flex-1 overflow-y-auto p-4 space-y-4 min-h-0 bg-[#1e1e1e]">
            {history.map((msg, i) => (
                <div key={i} className={`flex flex-col ${msg.role === 'user' ? 'items-end' : 'items-start'}`}>
                    <div className={`max-w-[90%] p-3 rounded-2xl text-xs shadow-sm ${msg.role === 'user' ? 'bg-blue-500 text-white rounded-tr-none' : 'bg-slate-700 text-slate-200 rounded-tl-none border border-slate-600'}`}>
                        <div className="whitespace-pre-wrap" dangerouslySetInnerHTML={{ __html: msg.content.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>') }} />
                        {msg.attachments && msg.attachments.length > 0 && (
                            <div className="flex flex-wrap gap-2 mt-2">
                                {msg.attachments.map((att, j) => (
                                    <div key={j} className="w-10 h-10 border border-white/20 rounded overflow-hidden bg-white/10 flex items-center justify-center">
                                        {att.type === 'image' ? (
                                            <img src={att.preview} alt="preview" className="w-full h-full object-cover" />
                                        ) : att.type === 'pdf' ? (
                                            <svg className="w-5 h-5 text-red-400" fill="currentColor" viewBox="0 0 24 24">
                                              <path d="M14,2H6A2,2 0 0,0 4,4V20A2,2 0 0,0 6,22H18A2,2 0 0,0 20,20V8L14,2M18,20H6V4H13V9H18V20Z" />
                                            </svg>
                                        ) : (
                                            <svg className="w-5 h-5 text-slate-300" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                                            </svg>
                                        )}
                                    </div>
                                ))}
                            </div>
                        )}
                    </div>
                </div>
            ))}
            <div ref={historyEndRef} />
        </div>
      </div>

      <div className="p-4 border-t border-slate-700 shrink-0 bg-slate-800">
        {attachments.length > 0 && (
          <div className="flex flex-wrap gap-2 mb-3">
            {attachments.map((att, i) => (
              <div key={i} className="relative group">
                <div className="w-12 h-12 border border-slate-600 rounded overflow-hidden bg-slate-700 flex items-center justify-center">
                  {att.type === 'image' ? (
                    <img src={att.preview} alt="preview" className="w-full h-full object-cover" />
                  ) : att.type === 'pdf' ? (
                    <svg className="w-6 h-6 text-red-500" fill="currentColor" viewBox="0 0 24 24">
                      <path d="M14,2H6A2,2 0 0,0 4,4V20A2,2 0 0,0 6,22H18A2,2 0 0,0 20,20V8L14,2M18,20H6V4H13V9H18V20Z" />
                    </svg>
                  ) : (
                    <svg className="w-6 h-6 text-slate-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                    </svg>
                  )}
                </div>
                <button 
                  onClick={() => removeAttachment(i)}
                  className="absolute -top-1 -right-1 bg-rose-500 text-white rounded-full w-4 h-4 flex items-center justify-center text-[10px] shadow-sm opacity-0 group-hover:opacity-100 transition-opacity"
                >
                  ×
                </button>
              </div>
            ))}
          </div>
        )}

        <form onSubmit={handleSubmit} className="relative">
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder={selectedEpic ? "Describe your product idea..." : "Select an Epic first..."}
            className="w-full pl-4 pr-12 py-3 border border-slate-600 rounded-xl focus:ring-2 focus:ring-blue-500 focus:border-transparent outline-none resize-none text-sm min-h-[80px] max-h-[150px] bg-slate-700 text-white placeholder:text-slate-500"
            style={{ color: '#ffffff', WebkitTextFillColor: '#ffffff' }}
            disabled={isGenerating || !selectedEpic}
          />
          <div className="absolute right-2 bottom-2 flex items-center gap-2">
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              className="p-2 text-slate-400 hover:text-blue-600 transition-colors"
              title="Attach files"
              disabled={isGenerating || !selectedEpic}
            >
              <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15.172 7l-6.586 6.586a2 2 0 102.828 2.828l6.414-6.414a4 4 0 00-5.656-5.656l-6.415 6.585a6 6 0 108.486 8.486L20.5 13" />
              </svg>
            </button>
            <button
              type="submit"
              disabled={isGenerating || !selectedEpic || (!input.trim() && attachments.length === 0)}
              className="p-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-all shadow-md"
            >
              {isGenerating ? (
                <div className="w-5 h-5 border-2 border-white/30 border-t-white rounded-full animate-spin" />
              ) : (
                <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8" />
                </svg>
              )}
            </button>
          </div>
          <input
            type="file"
            ref={fileInputRef}
            onChange={handleFileChange}
            multiple
            accept="image/*,application/pdf,.txt,.log"
            className="hidden"
          />
        </form>
      </div>
    </div>
  );
}
