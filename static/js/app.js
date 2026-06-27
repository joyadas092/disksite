function sharePost(url) {
    if (navigator.share) {
        navigator.share({ url });
    } else {
        navigator.clipboard.writeText(url).then(() => {
            const btn = event.target;
            const orig = btn.textContent;
            btn.textContent = "Copied!";
            setTimeout(() => { btn.textContent = orig; }, 1500);
        });
    }
}

const postsGrid = document.querySelector("#postsGrid");
const loadingState = document.querySelector("#loadingState");
const emptyState = document.querySelector("#emptyState");
const scrollSentinel = document.querySelector("#scrollSentinel");
const sortButtons = document.querySelectorAll("[data-sort]");

const state = {
    page: 1,
    sort: "latest",
    loading: false,
    hasMore: true,
};

function cardTemplate(post) {
    const thumbnail = post.thumbnail_url
        ? `<img src="${post.thumbnail_url}" alt="" loading="lazy">`
        : `<div class="thumb-placeholder">Diskwala</div>`;

    const links = post.links.map((_, index) => {
        const label = post.links.length === 1 ? "Watch Now" : `Part ${index + 1}`;
        return `<a class="btn btn-watch" href="/go/${post.id}/${index + 1}" target="_blank" rel="noopener noreferrer nofollow">${label}</a>`;
    }).join("");

    const shareUrl = `${location.origin}/go/${post.id}/1`;
    const shareBtn = `<button class="btn btn-share" onclick="sharePost('${shareUrl}')" type="button">Share</button>`;

    return `
        <article class="post-card">
            <div class="thumb-wrap">
                ${thumbnail}
                <span class="views-pill">${post.views} views</span>
            </div>
            <div class="post-body">
                <div class="link-actions">${links}</div>
                ${shareBtn}
            </div>
        </article>
    `;
}

async function loadPosts(reset = false) {
    if (state.loading || (!state.hasMore && !reset)) {
        return;
    }

    if (reset) {
        state.page = 1;
        state.hasMore = true;
        postsGrid.innerHTML = "";
        emptyState.classList.add("d-none");
    }

    state.loading = true;
    loadingState.classList.remove("d-none");

    const params = new URLSearchParams({
        sort: state.sort,
        page: state.page,
        limit: 20,
    });

    try {
        const response = await fetch(`/api/posts?${params.toString()}`);
        const data = await response.json();

        postsGrid.insertAdjacentHTML(
            "beforeend",
            data.posts.map(cardTemplate).join("")
        );

        state.page = data.next_page;
        state.hasMore = data.has_more && state.sort !== "random";

        if (!postsGrid.children.length) {
            emptyState.classList.remove("d-none");
        }
    } catch (error) {
        emptyState.textContent = "Could not load posts. Please refresh the page.";
        emptyState.classList.remove("d-none");
    } finally {
        state.loading = false;
        loadingState.classList.add("d-none");
    }
}

sortButtons.forEach((button) => {
    button.addEventListener("click", () => {
        sortButtons.forEach((item) => item.classList.remove("active"));
        button.classList.add("active");
        state.sort = button.dataset.sort;
        loadPosts(true);
    });
});

const observer = new IntersectionObserver((entries) => {
    if (entries[0].isIntersecting) {
        loadPosts();
    }
}, {
    rootMargin: "700px 0px",
});

observer.observe(scrollSentinel);
loadPosts(true);
