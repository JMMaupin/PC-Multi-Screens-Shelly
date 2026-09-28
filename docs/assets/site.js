// Every screenshot at the same scale.
//
// Stretched to fill its slot, a small message box ended up magnified and a
// large window shrunk: the app's text looked bigger in one than in the
// other. Here one screen pixel takes the same room everywhere, so the app
// reads at one size throughout. The largest screenshot, a 1102-pixel
// window, still fits the page at this scale; on a narrow screen, the
// max-width in the stylesheet still wins.
(function () {
  var SCALE = 0.85;
  function fit(img) {
    if (img.naturalWidth) {
      img.style.width = Math.round(img.naturalWidth * SCALE) + "px";
    }
  }
  var images = document.querySelectorAll(".shot img");
  for (var i = 0; i < images.length; i++) {
    var img = images[i];
    if (img.complete) {
      fit(img);
    } else {
      img.addEventListener("load", function (event) { fit(event.target); });
    }
  }
})();
