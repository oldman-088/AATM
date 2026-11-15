export type Language = 'en' | 'ar' | 'fa'

export interface Translations {
  appName: string
  appSubtitle: string
  nav: {
    dashboard: string
    chat: string
    agents: string
    tasks: string
    browsers: string
    providers: string
    projects: string
    git: string
    qa: string
    approvals: string
    settings: string
  }
  auth: {
    signIn: string
    signOut: string
    email: string
    password: string
    quickAdmin: string
    signedInAs: string
    authRequiredHint: string
  }
  common: {
    refresh: string
    create: string
    save: string
    cancel: string
    loading: string
    status: string
    actions: string
    search: string
    healthy: string
    degraded: string
    offline: string
    unknown: string
    idle: string
    working: string
    completed: string
    queued: string
    inProgress: string
    failed: string
    pending: string
    approved: string
    rejected: string
    runCycle: string
    selfHeal: string
    noData: string
  }
  dashboard: {
    title: string
    activeWorkers: string
    completionRate: string
    healthyProviders: string
    runningBrowsers: string
    pendingApprovals: string
    divisionsMatrix: string
    systemResources: string
    intelligence: string
    liveFeed: string
  }
  chat: {
    title: string
    placeholder: string
    send: string
    quickOrchestrate: string
    quickPlanTask: string
    quickProtectedMerge: string
  }
  agents: {
    title: string
    restart: string
    pause: string
    resume: string
    divisions: string
    completedCount: string
  }
  tasks: {
    title: string
    newTask: string
    taskTitle: string
    division: string
    priority: string
    complete: string
    failRetry: string
  }
  browsers: {
    title: string
    launchVisible: string
    launchBackground: string
    stop: string
    recover: string
    navigate: string
    createProfile: string
    capabilities: string
  }
  providers: {
    title: string
    healthCheckAll: string
    testDryRun: string
    usageSummary: string
    totalCost: string
    totalTokens: string
  }
  projects: {
    title: string
    newProject: string
    memoryTitle: string
    addMemory: string
    searchMemory: string
    scope: string
  }
  git: {
    title: string
    currentBranch: string
    protectedPolicy: string
    requestMerge: string
    worktrees: string
    recentCommits: string
  }
  qa: {
    title: string
    securityScan: string
    filesScanned: string
    findings: string
    triggerSelfHeal: string
  }
  approvals: {
    title: string
    approve: string
    reject: string
    auditTimeline: string
  }
  settings: {
    title: string
    language: string
    theme: string
    dark: string
    light: string
    notifications: string
    markRead: string
  }
}
