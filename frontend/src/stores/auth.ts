import { defineStore } from 'pinia'
import { ref } from 'vue'
import * as api from '@/api'
import type { LoginRequest, RegisterRequest } from '@/types'

export const useAuthStore = defineStore('auth', () => {
  const token = ref<string | null>(api.getToken())
  const email = ref<string | null>(api.getStoredEmail())
  const authMode = ref<'login' | 'register'>('login')
  const error = ref<string>('')

  const isAuthenticated = () => token.value !== null

  async function doRegister(req: RegisterRequest): Promise<string | null> {
    error.value = ''
    try {
      const r = await api.registerTenant(req)
      // 返回 api_key，调用方展示；不自动登录
      authMode.value = 'login'
      return r.api_key
    } catch (e) {
      error.value = (e as Error).message
      return null
    }
  }

  async function doLogin(req: LoginRequest): Promise<boolean> {
    error.value = ''
    try {
      const r = await api.login(req)
      token.value = r.access_token
      email.value = r.email
      api.saveAuth(r)
      return true
    } catch (e) {
      error.value = (e as Error).message
      return false
    }
  }

  function logout() {
    token.value = null
    email.value = null
    api.clearAuth()
  }

  return {
    token,
    email,
    authMode,
    error,
    isAuthenticated,
    doRegister,
    doLogin,
    logout,
  }
})
