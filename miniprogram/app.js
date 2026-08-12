const config = require('./config')

App({
  globalData: {
    config,
  },

  onLaunch() {
    if (config.useCloud && wx.cloud) {
      wx.cloud.init({
        env: config.cloudEnv,
        traceUser: true,
      })
    }
  },
})
