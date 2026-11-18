(function () {
  'use strict';

  const dataUrl = 'https://raw.githubusercontent.com/zwu88/zwu88.github.io/google-scholar-stats/gs_data.json';
  let dataPromise;

  function isCount(value) {
    return Number.isInteger(value) && value >= 0;
  }

  function validateData(data) {
    if (!data || typeof data !== 'object' || Array.isArray(data) ||
        !isCount(data.citedby) || !isCount(data.hindex) ||
        (data.status !== undefined && !['ok', 'cached'].includes(data.status)) ||
        /Google Scholar data will be populated/i.test(data.message || '') ||
        /^Test Author$/i.test(String(data.name || '').trim())) {
      throw new Error('Google Scholar statistics are unavailable');
    }
    return data;
  }

  async function fetchData() {
    const controller = new AbortController();
    const timeout = setTimeout(function () { controller.abort(); }, 15000);
    try {
      const response = await fetch(dataUrl, { signal: controller.signal });
      if (!response.ok) throw new Error('Failed to fetch Google Scholar statistics');
      return validateData(await response.json());
    } finally {
      clearTimeout(timeout);
    }
  }

  function setText(id, value) {
    const element = document.getElementById(id);
    if (element) element.textContent = value;
  }

  function updateTime(data) {
    const date = typeof data.updated === 'string' ? new Date(data.updated) : null;
    let text = date && !Number.isNaN(date.getTime()) ? date.toLocaleDateString('en-US', {
      year: 'numeric', month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit', timeZoneName: 'short'
    }) : 'Unavailable';
    if (data.status === 'cached' || /^Using cached data\b/i.test(data.message || '')) {
      text += ' (cached)';
    }
    setText('update-time', text);
  }

  function updatePublicationCitations(data) {
    const counts = new Map();
    const publications = data.publications;
    if (publications && typeof publications === 'object') {
      Object.entries(publications).forEach(function ([key, publication]) {
        if (publication && isCount(publication.num_citations)) {
          counts.set(publication.author_pub_id || key, publication.num_citations);
        }
      });
    }

    document.querySelectorAll('.publication-item').forEach(function (publication) {
      const periodical = publication.querySelector('.periodical');
      if (!periodical) return;
      const paperId = publication.getAttribute('data-paper-id');
      let citation = periodical.querySelector('[data-scholar-citations]');
      if (!counts.has(paperId)) {
        if (citation) citation.remove();
        return;
      }
      if (!citation) {
        citation = document.createElement('span');
        citation.setAttribute('data-scholar-citations', '');
        citation.style.cssText = 'color: #666; font-weight: normal; font-size: 0.9em; margin-left: 10px;';
        periodical.appendChild(citation);
      }
      citation.textContent = ` • ${counts.get(paperId).toLocaleString('en-US')} citations`;
    });
  }

  async function loadScholarStats() {
    try {
      if (!dataPromise) dataPromise = fetchData();
      const data = await dataPromise;
      setText('citations', data.citedby.toLocaleString('en-US'));
      setText('h-index', data.hindex.toLocaleString('en-US'));
      updateTime(data);
      updatePublicationCitations(data);
    } catch (error) {
      setText('citations', '—');
      setText('h-index', '—');
      setText('update-time', 'Unavailable');
      document.querySelectorAll('[data-scholar-citations]').forEach(function (citation) {
        citation.remove();
      });
      console.warn('Could not load Google Scholar data:', error);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', loadScholarStats, { once: true });
  } else {
    loadScholarStats();
  }
})();
