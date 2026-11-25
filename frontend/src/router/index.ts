import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import QualityVersioningView from '@/views/QualityVersioningView.vue'
import QuarantineDetailView from '@/views/QuarantineDetailView.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    {
      path: '/quality-versioning',
      name: 'quality-versioning',
      component: QualityVersioningView,
    },
    {
      path: '/quality-versioning/quarantine/:quarantineId',
      name: 'quarantine-detail',
      component: QuarantineDetailView,
    },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
