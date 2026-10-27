/*
Server Sent Events Extension
============================
This extension adds support for Server Sent Events to htmx.  See /www/extensions/sse.md for usage instructions.

*/

(function() {
  /** @type {import("../htmx").HtmxInternalApi} */
  var api

  htmx.defineExtension('sse', {

    /**
     * Init saves the provided reference to the internal HTMX API.
     *
     * @param {import("../htmx").HtmxInternalApi} api
     * @returns void
     */
    init: function(apiRef) {
      // store a reference to the internal API.
      api = apiRef

      // set a function in the public API for creating new EventSource objects
      if (htmx.createEventSource == undefined) {
        htmx.createEventSource = createEventSource
      }

      // Public re-entry so app code can revive a stream it deliberately
      // .close()d (e.g. after backgrounding a mobile PWA). A manual close fires
      // no onerror, so htmx's own reconnect never runs; calling this with a
      // non-zero retryCount routes through onopen's child re-registration, which
      // rebinds every sse-swap listener to the fresh EventSource.
      if (htmx.reconnectSSE == undefined) {
        htmx.reconnectSSE = function(elt, replace) {
          if (replace) {
            var source = api.getInternalData(elt).sseEventSource
            if (source) {
              api.triggerEvent(elt, 'htmx:sseClose', {
                source,
                type: 'reconnect',
              })
              source.close()
            }
          }
          ensureEventSourceOnElement(elt, 1)
        }
      }
    },

    getSelectors: function() {
      return ['[sse-connect]', '[data-sse-connect]', '[sse-swap]', '[data-sse-swap]']
    },

    /**
     * onEvent handles all events passed to this extension.
     *
     * @param {string} name
     * @param {Event} evt
     * @returns void
     */
    onEvent: function(name, evt) {
      var parent = evt.target || evt.detail.elt
      switch (name) {
        case 'htmx:beforeCleanupElement':
          var internalData = api.getInternalData(parent)
          // Try to remove remove an EventSource when elements are removed
          var source = internalData.sseEventSource
          if (source) {
            api.triggerEvent(parent, 'htmx:sseClose', {
              source,
              type: 'nodeReplaced',
            })
            internalData.sseEventSource.close()
          }

          return

        // Try to create EventSources when elements are processed
        case 'htmx:afterProcessNode':
          ensureEventSourceOnElement(parent)
      }
    }
  })

  /// ////////////////////////////////////////////
  // HELPER FUNCTIONS
  /// ////////////////////////////////////////////

  /**
   * createEventSource is the default method for creating new EventSource objects.
   * it is hoisted into htmx.config.createEventSource to be overridden by the user, if needed.
   *
   * @param {string} url
   * @returns EventSource
   */
  function createEventSource(url) {
    return new EventSource(url, { withCredentials: true })
  }

  /**
   * registerSSE looks for attributes that can contain sse events, right
   * now hx-trigger and sse-swap and adds listeners based on these attributes too
   * the closest event source
   *
   * @param {HTMLElement} elt
   */
  function registerSSE(elt) {
    // Add message handlers for every `sse-swap` attribute
    if (api.getAttributeValue(elt, 'sse-swap')) {
      // Find closest existing event source
      var sourceElement = api.getClosestMatch(elt, hasEventSource)
      if (sourceElement == null) {
        // api.triggerErrorEvent(elt, "htmx:noSSESourceError")
        return null // no eventsource in parentage, orphaned element
      }

      // Set internalData and source
      var internalData = api.getInternalData(sourceElement)
      var source = internalData.sseEventSource

      var sseSwapAttr = api.getAttributeValue(elt, 'sse-swap')
      var sseEventNames = sseSwapAttr.split(',')

      for (var i = 0; i < sseEventNames.length; i++) {
        const sseEventName = sseEventNames[i].trim()
        const listener = function(event) {
          // If the source is missing then close SSE
          if (maybeCloseSSESource(sourceElement)) {
            return
          }

          // If the body no longer contains the element, remove the listener
          if (!api.bodyContains(elt)) {
            source.removeEventListener(sseEventName, listener)
            return
          }

          // swap the response into the DOM and trigger a notification
          if (!api.triggerEvent(elt, 'htmx:sseBeforeMessage', event)) {
            return
          }
          swap(elt, event.data)
          if (sseEventName === 'transcript' && event.lastEventId) {
            var connectAttribute = sourceElement.hasAttribute('sse-connect')
              ? 'sse-connect' : 'data-sse-connect'
            var currentURL = new URL(
              sourceElement.getAttribute(connectAttribute), document.baseURI
            )
            currentURL.searchParams.set('after_seq', event.lastEventId)
            sourceElement.setAttribute(
              connectAttribute, currentURL.pathname + currentURL.search
            )
          }
          api.triggerEvent(elt, 'htmx:sseMessage', event)
        }

        // Register the new listener
        api.getInternalData(elt).sseEventListener = listener
        source.addEventListener(sseEventName, listener)
      }
    }

    // Add message handlers for every `hx-trigger="sse:*"` attribute
    if (api.getAttributeValue(elt, 'hx-trigger')) {
      // Find closest existing event source
      var sourceElement = api.getClosestMatch(elt, hasEventSource)
      if (sourceElement == null) {
        // api.triggerErrorEvent(elt, "htmx:noSSESourceError")
        return null // no eventsource in parentage, orphaned element
      }

      // Set internalData and source
      var internalData = api.getInternalData(sourceElement)
      var source = internalData.sseEventSource

      var triggerSpecs = api.getTriggerSpecs(elt)
      triggerSpecs.forEach(function(ts) {
        if (ts.trigger.slice(0, 4) !== 'sse:') {
          return
        }

        var listener = function (event) {
          if (maybeCloseSSESource(sourceElement)) {
            return
          }
          if (!api.bodyContains(elt)) {
            source.removeEventListener(ts.trigger.slice(4), listener)
          }
          // Trigger events to be handled by the rest of htmx
          htmx.trigger(elt, ts.trigger, event)
          htmx.trigger(elt, 'htmx:sseMessage', event)
        }

        // Register the new listener
        api.getInternalData(elt).sseEventListener = listener
        source.addEventListener(ts.trigger.slice(4), listener)
      })
    }
  }

  /**
   * ensureEventSourceOnElement creates a new EventSource connection on the provided element.
   * If a usable EventSource already exists, then it is returned.  If not, then a new EventSource
   * is created and stored in the element's internalData.
   * @param {HTMLElement} elt
   * @param {number} retryCount
   * @returns {EventSource | null}
   */
  function ensureEventSourceOnElement(elt, retryCount) {
    if (elt == null) {
      return null
    }

    // handle extension source creation attribute
    if (api.getAttributeValue(elt, 'sse-connect')) {
      var sseURL = api.getAttributeValue(elt, 'sse-connect')
      if (sseURL == null) {
        return
      }
      // AgentDeck: a page may add what it already shows to every (re)connect (#3228).
      if (typeof htmx.sseConnectUrl === 'function') {
        sseURL = htmx.sseConnectUrl(elt, sseURL)
      }

      ensureEventSource(elt, sseURL, retryCount)
    }

    registerSSE(elt)
  }

  function ensureEventSource(elt, url, retryCount) {
    var source = htmx.createEventSource(url)

    source.onerror = function(err) {
      // Log an error event
      api.triggerErrorEvent(elt, 'htmx:sseError', { error: err, source })

      // If parent no longer exists in the document, then clean up this EventSource
      if (maybeCloseSSESource(elt)) {
        return
      }

      // Otherwise, try to reconnect the EventSource
      if (source.readyState === EventSource.CLOSED) {
        retryCount = retryCount || 0
        retryCount = Math.max(Math.min(retryCount * 2, 128), 1)
        var timeout = retryCount * 500
        window.setTimeout(function() {
          ensureEventSourceOnElement(elt, retryCount)
        }, timeout)
      }
    }

    source.onopen = function(evt) {
      api.triggerEvent(elt, 'htmx:sseOpen', { source })

      if (retryCount && retryCount > 0) {
        const childrenToFix = elt.querySelectorAll("[sse-swap], [data-sse-swap], [hx-trigger], [data-hx-trigger]")
        for (let i = 0; i < childrenToFix.length; i++) {
          // AgentDeck: a child under a nested stream of its own belongs to that
          // stream. registerSSE binds to the closest source, so re-registering it
          // here would only add a second listener to the nested one (#2290).
          if (api.getClosestMatch(childrenToFix[i], hasEventSource) !== elt) continue
          registerSSE(childrenToFix[i])
        }
        // We want to increase the reconnection delay for consecutive failed attempts only
        retryCount = 0
      }
    }

    api.getInternalData(elt).sseEventSource = source


    var closeAttribute = api.getAttributeValue(elt, "sse-close");
    if (closeAttribute) {
      // close eventsource when this message is received
      source.addEventListener(closeAttribute, function() {
        api.triggerEvent(elt, 'htmx:sseClose', {
          source,
          type: 'message',
        })
        source.close()
      });
    }
  }

  /**
   * maybeCloseSSESource confirms that the parent element still exists.
   * If not, then any associated SSE source is closed and the function returns true.
   *
   * @param {HTMLElement} elt
   * @returns boolean
   */
  function maybeCloseSSESource(elt) {
    if (!api.bodyContains(elt)) {
      var source = api.getInternalData(elt).sseEventSource
      if (source != undefined) {
        api.triggerEvent(elt, 'htmx:sseClose', {
          source,
          type: 'nodeMissing',
        })
        source.close()
        // source = null
        return true
      }
    }
    return false
  }


  /**
   * @param {HTMLElement} elt
   * @param {string} content
   */
  function swap(elt, content) {
    api.withExtensions(elt, function(extension) {
      content = extension.transformResponse(content, null, elt)
    })

    var swapSpec = api.getSwapSpecification(elt)
    var target = api.getTarget(elt)
    // Pass the SSE element as the extension context. htmx.swap() otherwise has
    // no element from which to resolve custom swap extensions such as the keyed
    // session-list reconciler.
    api.swap(target, content, swapSpec, { contextElement: elt })
  }


  function hasEventSource(node) {
    return api.getInternalData(node).sseEventSource != null
  }
})()
