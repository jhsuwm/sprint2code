'use client';

import { useState, useEffect, useRef } from 'react';
import { useRouter } from 'next/navigation';
import ChatWindow from '@/components/ChatWindow';

export default function AutonomousDevDashboard() {
  const [showStoryList, setShowStoryList] = useState(false);
  const [jiraStructure, setJiraStructure] = useState<any[]>([]);
  const [loadingJira, setLoadingJira] = useState(false);
  const [selectedSpace, setSelectedSpace] = useState<any>(null);
  const [selectedEpic, setSelectedEpic] = useState<any>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [jobStatus, setJobStatus] = useState<any>(null);
  const [logs, setLogs] = useState<string[]>([]);
  const [isAuthenticated, setIsAuthenticated] = useState(true);
  const [showConfigModal, setShowConfigModal] = useState(false);
  const [configName, setConfigName] = useState('');
  const [configType, setConfigType] = useState<'frontend' | 'backend' | 'fullstack'>('fullstack');
  const [configContent, setConfigContent] = useState('');
  const [savingConfig, setSavingConfig] = useState(false);
  const [configs, setConfigs] = useState<any[]>([]);
  const [loadingConfigs, setLoadingConfigs] = useState(false);
  const [showConfigList, setShowConfigList] = useState(true);
  const [isEditMode, setIsEditMode] = useState(false);
  const [selectedConfigs, setSelectedConfigs] = useState<any[]>([]);
  const [stagedSelections, setStagedSelections] = useState<any[]>([]);
  const [frontendLogs, setFrontendLogs] = useState<string[]>([]);
  const [backendLogs, setBackendLogs] = useState<string[]>([]);
  const [appStatus, setAppStatus] = useState<string | null>(null);
  const [localConfigFiles, setLocalConfigFiles] = useState<any[]>([]);
  const [loadingLocalConfigs, setLoadingLocalConfigs] = useState(false);
  const logsEndRef = useRef<HTMLDivElement>(null);
  const frontendLogsEndRef = useRef<HTMLDivElement>(null);
  const backendLogsEndRef = useRef<HTMLDivElement>(null);
  const router = useRouter();

  const BACKEND_URL = process.env.NEXT_PUBLIC_BACKEND_URL || 'http://localhost:8000';

  // Restore context on load
  useEffect(() => {
    const savedSpace = localStorage.getItem('selectedSpace');
    const savedEpic = localStorage.getItem('selectedEpic');
    const savedConfigsStr = localStorage.getItem('selectedConfigs');

    if (savedSpace) setSelectedSpace(JSON.parse(savedSpace));
    if (savedEpic) setSelectedEpic(JSON.parse(savedEpic));
    if (savedConfigsStr) setSelectedConfigs(JSON.parse(savedConfigsStr));
  }, []);

  const getAuthToken = () => {
    // For OSS version, no authentication required
    // You can add your own auth system if needed
    return 'demo-token';
  };

  const scrollToBottom = () => {
    logsEndRef.current?.scrollIntoView({ behavior: "auto" });
  };

  useEffect(() => {
    scrollToBottom();
  }, [logs]);

  useEffect(() => {
    if (frontendLogs.length > 0) {
      frontendLogsEndRef.current?.scrollIntoView({ behavior: "auto" });
    }
  }, [frontendLogs]);

  useEffect(() => {
    if (backendLogs.length > 0) {
      backendLogsEndRef.current?.scrollIntoView({ behavior: "auto" });
    }
  }, [backendLogs]);

  const fetchJiraStructure = async (shouldShowModal = true) => {
    setLoadingJira(true);
    if (shouldShowModal) setShowStoryList(true);
    try {
      const token = getAuthToken();
      const res = await fetch(`${BACKEND_URL}/autonomous-dev/structure`, {
        headers: { 'Authorization': `Bearer ${token}` }
      });
      if (res.ok) {
        const data = await res.json();
        setJiraStructure(data);
      }
    } catch (e) {
      console.error(e);
    } finally {
      setLoadingJira(false);
    }
  };

  const startGeneration = async (storyId: string) => {
    try {
      const token = getAuthToken();
      const res = await fetch(`${BACKEND_URL}/autonomous-dev/generate`, {
        method: 'POST',
        headers: { 
          'Authorization': `Bearer ${token}`,
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({
          story_id: storyId,
          skill_names: selectedConfigs.map(c => c.name),
          config_name: selectedConfigs.find(c => c.type === 'grouped')?.name,
          frontend_config_name: selectedConfigs.find(c => c.type === 'frontend')?.name,
          backend_config_name: selectedConfigs.find(c => c.type === 'backend')?.name,
        })
      });
      if (res.ok) {
        const data = await res.json();
        setJobId(data.job_id);
        pollProgress(data.job_id);
      }
    } catch (e) {
      console.error(e);
    }
  };

  const pollProgress = (currentJobId: string) => {
    const interval = setInterval(async () => {
      try {
        const token = getAuthToken();
        const res = await fetch(`${BACKEND_URL}/autonomous-dev/progress/${currentJobId}`, {
          headers: { 'Authorization': `Bearer ${token}` }
        });
        if (res.ok) {
          const data = await res.json();
          setJobStatus(data);
          setLogs(data.logs || []);
          setFrontendLogs(data.frontend_logs || []);
          setBackendLogs(data.backend_logs || []);
          setAppStatus(data.app_status || null);
          if (data.status === 'COMPLETED' || data.status === 'FAILED') {
            clearInterval(interval);
          }
        }
      } catch (e) {
        console.error(e);
      }
    }, 5000);
  };

  const fetchConfigs = async () => {
    setLoadingConfigs(true);
    try {
      const token = getAuthToken();
      const res = await fetch(`${BACKEND_URL}/autonomous-dev/configs`, {
        headers: { 'Authorization': `Bearer ${token}` }
      });
      if (res.ok) {
        const data = await res.json();
        setConfigs(data);
        setStagedSelections(selectedConfigs.map(config => config.name));
      }
    } catch (e) {
      console.error(e);
    } finally {
      setLoadingConfigs(false);
    }
  };

  const handleSaveConfig = async () => {
    if (!configName.trim() || !configContent.trim()) {
      alert('Please provide config name and content');
      return;
    }

    const typeToSave = configType === 'fullstack' ? 'grouped' : configType;
    setSavingConfig(true);

    try {
      const token = getAuthToken();
      const payload = {
        name: configName.trim(),
        type: typeToSave,
        content: configContent.trim()
      };

      const res = await fetch(`${BACKEND_URL}/autonomous-dev/config`, {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${token}`,
          'Content-Type': 'application/json'
        },
        body: JSON.stringify(payload)
      });
      
      if (res.ok) {
        alert('Config saved successfully!');
        const newConfig = { name: configName.trim(), type: typeToSave };
        setSelectedConfigs([newConfig]);
        setStagedSelections([configName.trim()]);
        localStorage.setItem('selectedConfigs', JSON.stringify([newConfig]));
        closeConfigModal();
        fetchConfigs();
      } else {
        const error = await res.json();
        alert(`Failed to save config: ${error.detail || 'Unknown error'}`);
      }
    } catch (e) {
      console.error(e);
      alert('Failed to save config. Please try again.');
    } finally {
      setSavingConfig(false);
    }
  };

  const handlePRDGenerated = (prd: string, attachments: File[], projectKey?: string, epicKey?: string, storyId?: string) => {
    setShowStoryList(false);
    if (storyId) {
      startGeneration(storyId);
    }
    const token = getAuthToken();
    fetch(`${BACKEND_URL}/autonomous-dev/structure`, {
      headers: { 'Authorization': `Bearer ${token}` }
    }).then(res => res.json()).then(data => setJiraStructure(data)).catch(console.error);
  };

  const handleSelectEpic = (space: any, epic: any) => {
    setSelectedSpace(space);
    setSelectedEpic(epic);
    localStorage.setItem('selectedSpace', JSON.stringify(space));
    localStorage.setItem('selectedEpic', JSON.stringify(epic));
    setShowStoryList(false);
  };

  const openConfigModal = async () => {
    setShowConfigModal(true);
    setShowConfigList(true);
    setIsEditMode(false);
    setStagedSelections(selectedConfigs.map(c => c.name));
    setConfigName('');
    setConfigContent('');
    
    // Fetch local config files immediately
    setLoadingLocalConfigs(true);
    try {
      const token = getAuthToken();
      const res = await fetch(`${BACKEND_URL}/autonomous-dev/local-skills`, {
        headers: { 'Authorization': `Bearer ${token}` }
      });
      if (res.ok) {
        const files = await res.json();
        setLocalConfigFiles(files);
      } else {
        setLocalConfigFiles([]);
      }
    } catch (e) {
      console.error(e);
      setLocalConfigFiles([]);
    } finally {
      setLoadingLocalConfigs(false);
    }
  };

  const closeConfigModal = () => {
    setShowConfigModal(false);
    setShowConfigList(true);
    setIsEditMode(false);
    setStagedSelections(selectedConfigs.map(c => c.name));
    setConfigName('');
    setConfigContent('');
  };

  const handleSelectCheckedConfigs = () => {
    const newSelected = configs.filter(config => stagedSelections.includes(config.name));
    setSelectedConfigs(newSelected);
    localStorage.setItem('selectedConfigs', JSON.stringify(newSelected));
    closeConfigModal();
  };

  const handleAddNewConfig = async () => {
    // Fetch local config files
    setLoadingLocalConfigs(true);
    try {
      const token = getAuthToken();
      const res = await fetch(`${BACKEND_URL}/autonomous-dev/local-skills`, {
        headers: { 'Authorization': `Bearer ${token}` }
      });
      if (res.ok) {
        const files = await res.json();
        setLocalConfigFiles(files);
      } else {
        setLocalConfigFiles([]);
      }
    } catch (e) {
      console.error(e);
      setLocalConfigFiles([]);
    } finally {
      setLoadingLocalConfigs(false);
    }
    
    setStagedSelections([]);
    setConfigName('');
    setConfigType('fullstack');
    setConfigContent('');
    setIsEditMode(false);
    setShowConfigList(false);
  };

  const handleSelectLocalConfigFile = (file: any) => {
    // Auto-detect type from filename
    let detectedType: 'frontend' | 'backend' | 'fullstack' = 'fullstack';
    if (file.type === 'frontend' || file.name.includes('frontend')) {
      detectedType = 'frontend';
    } else if (file.type === 'backend' || file.name.includes('backend')) {
      detectedType = 'backend';
    }
    
    const configType = detectedType === 'fullstack' ? 'grouped' : detectedType;
    
    // Create new config object
    const newConfig = { 
      name: file.name,
      type: configType,
      local_skill: file.name
    };
    
    // Remove any existing config of the same type, then add the new one
    const updatedConfigs = [
      ...selectedConfigs.filter(c => c.type !== configType),
      newConfig
    ];
    
    setSelectedConfigs(updatedConfigs);
    localStorage.setItem('selectedConfigs', JSON.stringify(updatedConfigs));
    
    // Don't close modal - let user select more configs
    // Show a success message instead
    alert(`✓ ${file.name} added! You can select more configs or close this dialog.`);
  };

  const backToConfigList = () => {
    setShowConfigList(true);
    setIsEditMode(false);
    setStagedSelections(selectedConfigs.map(c => c.name));
    setConfigName('');
    setConfigContent('');
  };

  if (!isAuthenticated) return null;

  return (
    <div className="h-screen bg-slate-950 flex flex-col overflow-hidden">
      {/* Header */}
      <header className="bg-slate-900 border-b border-slate-700 p-2 shadow-sm flex items-center justify-between relative shrink-0">
        <div className="flex items-center gap-3">
          <h1 className="text-xl font-bold text-slate-200">🚀 Orion Dev Orchestrator</h1>
          <button 
            onClick={() => fetchJiraStructure(true)}
            className="bg-gradient-to-r from-blue-600 to-indigo-600 hover:from-blue-700 hover:to-indigo-700 text-white px-4 py-1.5 rounded-full text-xs font-semibold transition-all duration-300 shadow-lg hover:shadow-xl transform hover:-translate-y-0.5 flex items-center"
          >
            <svg className="w-4 h-4 mr-1.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2" />
            </svg>
            JIRA Epic
          </button>
          <button 
            onClick={openConfigModal}
            className="bg-gradient-to-r from-purple-600 to-violet-600 hover:from-purple-700 hover:to-violet-700 text-white px-4 py-1.5 rounded-full text-xs font-semibold transition-all duration-300 shadow-lg hover:shadow-xl transform hover:-translate-y-0.5 flex items-center group min-w-[140px] max-w-[280px]"
            title={selectedConfigs.length > 0 ? `Selected: ${selectedConfigs.map(c => c.name).join(', ')}` : 'Select Agent Skills'}
          >
            <svg className="w-4 h-4 mr-1.5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 20l4-16m4 4l4 4-4 4M6 16l-4-4 4-4" />
            </svg>
            <span className="truncate">
              {selectedConfigs.length > 0 ? selectedConfigs.map(c => c.name).join(', ') : 'Agent Skills'}
            </span>
          </button>
        </div>
      </header>

      <main className="flex-1 p-6 overflow-hidden flex flex-row gap-4 relative">
        {/* Left Panel - Chat */}
        <div className="w-1/4 flex flex-col h-full overflow-hidden">
          <ChatWindow 
            onPRDGenerated={handlePRDGenerated} 
            selectedEpic={selectedEpic} 
            selectedSpace={selectedSpace} 
            selectedConfigs={selectedConfigs}
          />
        </div>
        
        {/* Center Panel - Agent Logs */}
        <div className="w-1/2 flex flex-col h-full overflow-hidden">
          <div className="flex-1 flex flex-col overflow-hidden min-h-0">
            <div className="bg-slate-900 rounded-2xl shadow-sm border border-slate-700 flex flex-col overflow-hidden h-full">
              <div className="p-3 border-b border-slate-700 bg-slate-800 flex justify-between items-center shrink-0">
                <h3 className="font-semibold text-slate-200 text-sm flex items-center">
                  <svg className="w-4 h-4 mr-2 text-slate-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10" />
                  </svg>
                  Agent Execution Logs
                </h3>
                {jobStatus && (
                  <span className={`px-2 py-1 rounded-full text-xs font-semibold ${jobStatus.status === 'RUNNING' ? 'bg-blue-100 text-blue-700' : jobStatus.status === 'COMPLETED' ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-700'}`}>
                    {jobStatus.status}
                  </span>
                )}
              </div>
              <div className="flex-1 overflow-y-auto p-3 font-mono text-xs space-y-1 bg-[#1e1e1e] text-slate-300">
                {logs.length === 0 ? (
                  <div className="h-full flex items-center justify-center text-slate-500 italic">
                    <p>Select JIRA Epic and describe product idea to start</p>
                  </div>
                ) : (
                  <>
                    {logs.map((log, i) => {
                      // Determine color based on log content
                      let colorClass = 'text-green-400'; // Default: success/info
                      if (log.includes('[ERROR]') || log.includes('❌') || log.includes('FAILED') || log.includes('Error:')) {
                        colorClass = 'text-red-400';
                      } else if (log.includes('[WARNING]') || log.includes('⚠️')) {
                        colorClass = 'text-yellow-400';
                      }
                      return <div key={i} className={`font-mono text-xs ${colorClass}`}>{log}</div>;
                    })}
                    <div ref={logsEndRef} />
                  </>
                )}
              </div>
            </div>
          </div>
        </div>
        
        {/* Right Panel - App Logs */}
        <div className="w-1/4 flex flex-col gap-4 h-full overflow-hidden">
          {appStatus && (
            <div className="flex flex-col gap-2">
              <div className={`px-4 py-2 rounded-lg font-semibold text-center ${appStatus === 'HEALTHY' ? 'bg-green-900/30 text-green-300' : 'bg-red-900/30 text-red-300'}`}>
                App: {appStatus}
              </div>
              {(appStatus === 'VALIDATION_FAILED' || appStatus === 'STARTUP_FAILED') && jobId && (
                <button
                  onClick={async () => {
                    try {
                      const token = getAuthToken();
                      const res = await fetch(`${BACKEND_URL}/autonomous-dev/rerun-deployment/${jobId}`, {
                        method: 'POST',
                        headers: { 'Authorization': `Bearer ${token}` }
                      });
                      if (res.ok) {
                        const data = await res.json();
                        alert('✅ Deployment rerun started! Check logs for progress.');
                        // Continue polling to see the updated status
                        pollProgress(jobId);
                      } else {
                        const error = await res.json();
                        alert(`❌ Failed to rerun: ${error.detail || 'Unknown error'}`);
                      }
                    } catch (e) {
                      console.error(e);
                      alert('❌ Failed to rerun deployment. Please try again.');
                    }
                  }}
                  className="bg-gradient-to-r from-blue-600 to-indigo-600 hover:from-blue-700 hover:to-indigo-700 text-white px-4 py-2 rounded-lg text-sm font-semibold transition-all duration-300 shadow-lg hover:shadow-xl transform hover:-translate-y-0.5 flex items-center justify-center"
                >
                  <svg className="w-4 h-4 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                  </svg>
                  Rerun Deployment
                </button>
              )}
            </div>
          )}
          
          {/* Frontend Logs */}
          <div className="flex-1 flex flex-col overflow-hidden min-h-0">
            <div className="bg-slate-900 rounded-2xl shadow-sm border border-slate-700 flex flex-col overflow-hidden h-full">
              <div className="p-3 border-b border-slate-700 bg-slate-800">
                <h3 className="font-semibold text-slate-200 text-sm">Frontend Log</h3>
              </div>
              <div className="flex-1 overflow-y-auto p-3 font-mono text-xs bg-[#1e1e1e] text-slate-300">
                {frontendLogs.length === 0 ? (
                  <div className="h-full flex items-center justify-center text-slate-500 italic">No logs yet</div>
                ) : (
                  <>
                    {frontendLogs.map((log, i) => {
                      // Determine color based on log content
                      let colorClass = 'text-green-300'; // Default: success/info
                      if (log.includes('ERROR') || log.includes('❌') || log.includes('FAILED') || log.includes('Error:') || log.includes('error')) {
                        colorClass = 'text-red-400';
                      } else if (log.includes('WARNING') || log.includes('⚠️') || log.includes('warn')) {
                        colorClass = 'text-yellow-400';
                      }
                      return <div key={i} className={`font-mono text-xs ${colorClass}`}>{log}</div>;
                    })}
                    <div ref={frontendLogsEndRef} />
                  </>
                )}
              </div>
            </div>
          </div>
          
          {/* Backend Logs */}
          <div className="flex-1 flex flex-col overflow-hidden min-h-0">
            <div className="bg-slate-900 rounded-2xl shadow-sm border border-slate-700 flex flex-col overflow-hidden h-full">
              <div className="p-3 border-b border-slate-700 bg-slate-800">
                <h3 className="font-semibold text-slate-200 text-sm">Backend Log</h3>
              </div>
              <div className="flex-1 overflow-y-auto p-3 font-mono text-xs bg-[#1e1e1e] text-slate-300">
                {backendLogs.length === 0 ? (
                  <div className="h-full flex items-center justify-center text-slate-500 italic">No logs yet</div>
                ) : (
                  <>
                    {backendLogs.map((log, i) => {
                      // Determine color based on log content
                      let colorClass = 'text-green-300'; // Default: success/info
                      if (log.includes('ERROR') || log.includes('❌') || log.includes('FAILED') || log.includes('Error:') || log.includes('error')) {
                        colorClass = 'text-red-400';
                      } else if (log.includes('WARNING') || log.includes('⚠️') || log.includes('warn')) {
                        colorClass = 'text-yellow-400';
                      }
                      return <div key={i} className={`font-mono text-xs ${colorClass}`}>{log}</div>;
                    })}
                    <div ref={backendLogsEndRef} />
                  </>
                )}
              </div>
            </div>
          </div>
        </div>
      </main>

      {/* Config Modal - Simplified version */}
      {showConfigModal && (
        <div className="fixed inset-0 bg-black/60 backdrop-blur-sm z-50 flex items-center justify-center">
          <div className="bg-white w-full max-w-3xl max-h-[90vh] shadow-2xl rounded-2xl overflow-hidden flex flex-col">
            <div className="flex justify-between items-center p-6 border-b">
              <h2 className="text-2xl font-bold">Agent Skills</h2>
              <button onClick={closeConfigModal} className="text-slate-400 hover:text-slate-600">
                <svg className="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" /></svg>
              </button>
            </div>
            <div className="overflow-y-auto flex-1 p-6">
              <div>
                <h3 className="text-lg font-bold text-slate-800 mb-1">Available Skills</h3>
                <p className="text-sm text-slate-600 mb-4">Click on a skill to toggle selection. Selected skills will be used during code generation and auto-fix.</p>
                
                {loadingLocalConfigs ? (
                  <div className="flex justify-center items-center py-10">
                    <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-purple-600"></div>
                  </div>
                ) : localConfigFiles.length === 0 ? (
                  <div className="text-center py-10 text-slate-500 bg-slate-50 rounded-lg border-2 border-dashed border-slate-300">
                    <svg className="w-12 h-12 mx-auto mb-3 text-slate-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                    </svg>
                    <p className="text-sm font-semibold">No skills found</p>
                    <p className="text-xs mt-1">Add SKILL.md files under config/skills</p>
                  </div>
                ) : (
                  <div className="space-y-2 max-h-[500px] overflow-y-auto">
                    {localConfigFiles.map((file) => {
                      const isBackend = file.name.includes('backend');
                      const isFrontend = file.name.includes('frontend');
                      const configTypeLabel = isBackend ? 'Backend' : isFrontend ? 'Frontend' : 'Full-Stack';
                      const configType = isBackend ? 'backend' : isFrontend ? 'frontend' : 'grouped';
                      
                      // Check if this file is already selected
                      const isSelected = selectedConfigs.some(c => c.name === file.name);
                      const baseColorClass = isBackend ? 'border-green-300 bg-green-50' : isFrontend ? 'border-blue-300 bg-blue-50' : 'border-purple-300 bg-purple-50';
                      const selectedColorClass = isBackend ? 'border-green-500 bg-green-100' : isFrontend ? 'border-blue-500 bg-blue-100' : 'border-purple-500 bg-purple-100';
                      const badgeClass = isBackend ? 'bg-green-100 text-green-700' : isFrontend ? 'bg-blue-100 text-blue-700' : 'bg-purple-100 text-purple-700';
                      
                      return (
                        <div
                          key={file.name}
                          className={`w-full p-4 border-2 ${isSelected ? selectedColorClass : baseColorClass} rounded-lg transition-all ${isSelected ? '' : 'hover:shadow-md cursor-pointer'}`}
                        >
                          <div className="flex items-start justify-between gap-3">
                            <div 
                              className="flex-1 cursor-pointer"
                              onClick={() => {
                                if (!isSelected) {
                                  // Select
                                  const newConfig = { 
                                    name: file.name,
                                    type: configType,
                                    local_skill: file.name
                                  };
                                  // Remove existing config of same type, add new one
                                  const updatedConfigs = [
                                    ...selectedConfigs.filter(c => c.type !== configType),
                                    newConfig
                                  ];
                                  setSelectedConfigs(updatedConfigs);
                                  localStorage.setItem('selectedConfigs', JSON.stringify(updatedConfigs));
                                }
                              }}
                            >
                              <div className="flex items-center gap-2 mb-1">
                                {isSelected ? (
                                  <svg className="w-5 h-5 text-green-600" fill="currentColor" viewBox="0 0 24 24">
                                    <path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/>
                                  </svg>
                                ) : (
                                  <svg className="w-5 h-5 text-slate-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                                  </svg>
                                )}
                                <span className={`font-semibold ${isSelected ? 'text-slate-900' : 'text-slate-700'}`}>{file.name}</span>
                                {isSelected && (
                                  <span className="ml-2 px-2 py-0.5 rounded-full text-xs font-bold bg-green-600 text-white">
                                    SELECTED
                                  </span>
                                )}
                              </div>
                              <p className="text-xs text-slate-500 ml-7">{file.path}</p>
                            </div>
                            <div className="flex items-center gap-2 shrink-0">
                              <span className={`px-2 py-1 rounded-full text-xs font-semibold ${badgeClass}`}>
                                {configTypeLabel}
                              </span>
                              {isSelected && (
                                <button
                                  onClick={() => {
                                    // Deselect
                                    const updatedConfigs = selectedConfigs.filter(c => c.name !== file.name);
                                    setSelectedConfigs(updatedConfigs);
                                    localStorage.setItem('selectedConfigs', JSON.stringify(updatedConfigs));
                                  }}
                                  className="p-1.5 hover:bg-red-100 rounded-lg transition-colors group"
                                  title="Remove this skill"
                                >
                                  <svg className="w-4 h-4 text-slate-600 group-hover:text-red-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                                  </svg>
                                </button>
                              )}
                            </div>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            </div>
          </div>
        </div>
      )}

      {/* JIRA Epic Selection Modal */}
      {showStoryList && (
        <div className="fixed inset-0 bg-black/60 backdrop-blur-sm z-50 flex justify-start">
          <div className="bg-white w-full max-w-md h-full shadow-2xl p-6 overflow-y-auto">
            <div className="flex justify-between items-center mb-6">
              <h2 className="text-2xl font-bold text-slate-900">Select Epic</h2>
              <button onClick={() => setShowStoryList(false)} className="text-slate-400 hover:text-slate-600">
                <svg className="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" /></svg>
              </button>
            </div>
            {loadingJira ? (
              <div className="flex justify-center py-10"><div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600"></div></div>
            ) : (
              <div className="space-y-6">
                {jiraStructure.map(project => (
                  <div key={project.id}>
                    <div className="font-bold mb-2 text-slate-900">{project.name}</div>
                    {project.epics.map((epic: any) => (
                      <div key={epic.id} onClick={() => handleSelectEpic(project, epic)} className="border-2 border-slate-200 rounded-lg p-4 hover:border-blue-500 hover:bg-blue-50 cursor-pointer mb-2 transition-all">
                        <h3 className="font-semibold text-slate-900">{epic.fields.summary}</h3>
                        <div className="text-xs text-slate-500">{epic.key}</div>
                      </div>
                    ))}
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
