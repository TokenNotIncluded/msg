"""The homepage illustration must remain accessible and browser-only."""

import shutil
import subprocess
from html.parser import HTMLParser
from importlib.resources import files

import pytest

from msg.transports.home_art import HERO_SCRIPT, TOKEN_HERO
from msg.transports.home_page import HOME_BROWSER_HEADERS, home_html
from msg.transports.public_board import HASH as PUBLIC_BOARD_HASH


class HeroElements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def test_shared_home_region_and_isolated_art_remain_accessible():
    html = home_html().decode()
    parsed = HeroElements(html)
    regions = [
        attrs
        for tag, attrs in parsed.tags
        if tag == 'section' and attrs.get('id') == 'public-board'
    ]
    assert len(regions) == 1
    assert regions[0].get('aria-label') == '公共栏 / Shared board'
    assert any(
        tag == 'summary' and attrs.get('aria-label') == '公共栏选项 / Shared board options'
        for tag, attrs in parsed.tags
    )
    assert 'class="token-cloud"' not in html
    images = [attrs for tag, attrs in parsed.tags if tag == 'img']
    assert any(
        attrs.get('src', '').startswith('/_public-board/art.svg')
        and attrs.get('alt')
        and attrs.get('width') == '960'
        and attrs.get('height') == '300'
        for attrs in images
    )
    assert any(tag == 'button' and 'data-pause' in attrs for tag, attrs in parsed.tags)
    assert f"'sha256-{PUBLIC_BOARD_HASH}'" in HOME_BROWSER_HEADERS['Content-Security-Policy']


def test_motion_is_opt_in_and_pauses_outside_the_visible_page():
    html = home_html().decode()
    css = html.split('<style>')[1].split('</style>')[0]
    base, motion = css.split('@media (prefers-reduced-motion: no-preference)')
    assert 'animation: token-assemble' not in base
    assert 'animation-play-state: paused' in motion
    assert '.token-art[data-running=true]' in motion
    assert 'IntersectionObserver' in HERO_SCRIPT and '!document.hidden' in HERO_SCRIPT
    assert '!motion.matches' in HERO_SCRIPT


def test_particles_are_bounded_and_art_uses_a_mobile_viewbox():
    parsed = HeroElements(TOKEN_HERO)
    particles = [attrs for tag, attrs in parsed.tags if tag == 'text']
    assert 40 <= len(particles) < 120
    assert all(0 < int(p['x']) < 720 and 0 < int(p['y']) < 280 for p in particles)
    assert any(tag == 'svg' and attrs.get('viewbox') == '0 0 720 280' for tag, attrs in parsed.tags)


@pytest.mark.skipif(shutil.which('node') is None, reason='Node is required for browser game logic')
def test_packaged_ascii_build_world_and_physical_ads():
    """Check edits, reach, body collisions, support, reset and actual poster geometry."""
    html = files('msg.data').joinpath('lightjunction-ascii.html').read_text()
    functions = html.split('<script>', 1)[1].split('(() => {', 1)[0]
    script = r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const sandbox={Math,Number,Array,Object,Map,Set};vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(0,'utf8'),sandbox);
function sphere(cx,cy,cz,rx,ry,rz,shade,nu,nv){
  const points=[];
  for(let v=1;v<nv;v++)for(let u=0;u<nu;u++){
    const a=Math.PI*v/nv,b=Math.PI*2*u/nu,n=[Math.sin(a)*Math.cos(b),Math.cos(a),Math.sin(a)*Math.sin(b)];
    const normal=n.map((x,i)=>x/[rx,ry,rz][i]),len=Math.hypot(...normal);
    points.push([cx+rx*n[0],cy+ry*n[1],cz+rz*n[2],...normal.map(x=>x/len),shade]);
  }
  return points;
}
let terrainSamples=0;
const flat=()=>{terrainSamples++;return 0;};
const world=sandbox.createAsciiBuildWorld({terrain:flat,sphere});
const distantPlayer={x:0,z:0,feet:0,eye:2.06,radius:.24};
const values=x=>JSON.parse(JSON.stringify(x));
const down=(x,z,y=3)=>world.raycast([x,y,z],[0,-1,0]);
let checks=0;
function check(name,run){run();checks++;console.log('PASS '+name);}
check('terrain first block, upper face stacking, side face adjacency and materials',()=>{
  const hit=down(4.5,2.5);assert.equal(hit.kind,'ground');assert.equal(hit.removable,false);
  assert.ok(Math.abs(hit.point[1])<.001);assert.deepEqual(values(hit.normal),[0,1,0]);
  assert.equal(world.place(hit,'grass',distantPlayer).ok,true);
  assert.ok(world.points(4,2).some(p=>p[7]==='grass'));
  const top=down(4.5,2.5);assert.deepEqual(values(top.cell),[4,0,2]);assert.equal(top.material,'grass');
  assert.equal(world.place(top,'stone',distantPlayer).ok,true);
  assert.equal(world.floor(4.5,2.5,0),2);
  const side=world.raycast([3,.5,2.5],[1,0,0]);assert.deepEqual(values(side.normal),[-1,0,0]);
  assert.equal(world.place(side,'wood',distantPlayer).ok,true);
  assert.equal(world.count,3);
  assert.equal(world.raycast([3.5,2,2.5],[0,-1,0]).material,'wood');
  const points=world.points(4,2);
  assert.ok(points.some(p=>p[7]==='dirt')&&points.some(p=>p[7]==='stone')&&points.some(p=>p[7]==='wood'));
});
check('one-metre walls block movement, while their upper surface supports a standing player',()=>{
  assert.equal(world.blocked(4.5,2.5,0),true);
  assert.equal(world.blocked(4.5,2.5,2),false);
  assert.equal(world.blocked(3.5,2.5,1),false);
  assert.equal(world.blocked(2.70,2.5,0),false);
  assert.equal(world.blocked(2.82,2.5,0),true);
  assert.equal(world.floor(2.77,2.5,0),1);
  assert.equal(world.floor(2.75,2.5,0),0);
});
check('editing rejects a cube crossing either body or head; body edge uses a circle',()=>{
  world.reset();const hit=down(4.5,2.5);
  assert.equal(world.place(hit,'dirt',{x:4.5,z:2.5,feet:0,eye:2.06}).ok,false);
  assert.equal(world.place(hit,'dirt',{x:3.9,z:2.5,feet:0,eye:2.06}).ok,false);
  assert.equal(world.place(hit,'dirt',distantPlayer).ok,true);
  const top=down(4.5,2.5);
  assert.equal(world.place(top,'stone',{x:4.5,z:2.5,feet:0,eye:2.06}).ok,false);
});
check('reach caps long rays, normalizes direction, and refuses invalid input',()=>{
  assert.equal(world.raycast([4.5,7,2.5],[0,-1,0],100),null);
  assert.equal(world.raycast([4.5,3,2.5],[0,-100,0]).kind,'block');
  assert.equal(world.raycast([4,3,2],[0,0,0]),null);
  assert.equal(world.raycast([4,NaN,2],[0,-1,0]),null);
  assert.equal(world.raycast([4,3,2],[0,-1,0],Infinity),null);
  assert.equal(world.raycast([4,3,2],[0,-1,0],.5),null);
});
check('a suspended cube is a ceiling rather than a floor for a player walking below it',()=>{
  const roof=sandbox.createAsciiBuildWorld({terrain:()=>0,sphere});
  for(let i=0;i<4;i++)assert.equal(roof.place(roof.raycast([5.5,4.9,5.5],[0,-1,0]),'stone',distantPlayer).ok,true);
  for(let i=0;i<3;i++)assert.equal(roof.remove(roof.raycast([4,i+.5,5.5],[1,0,0])).ok,true);
  assert.equal(roof.count,1);assert.equal(roof.blocked(5.5,5.5,0),false);
  assert.equal(roof.floor(5.5,5.5,0,.5),0);
  assert.equal(roof.floor(5.5,5.5,0,4),4);
  assert.equal(roof.ceiling(5.5,5.5,2.06),3);
  assert.equal(roof.ceiling(4.70,5.5,2.06),Infinity);
  assert.equal(roof.ceiling(5.5,5.5,4.06),Infinity);
});
check('only the local shelter/stair footprint is protected; remote terrain remains editable',()=>{
  assert.equal(down(0,-3),null);
  const fabricated={point:[0,0,-3],normal:[0,1,0],cell:[0,-1,-3],distance:3,kind:'ground'};
  assert.equal(world.place(fabricated,'grass',distantPlayer).ok,false);
  assert.equal(down(-2.5,-3),null);
  const outside=down(-5.5,-3);assert.equal(outside.kind,'ground');
  assert.equal(world.place(outside,'dirt',distantPlayer).ok,true);
  assert.equal(world.remove(down(-5.5,-3)).ok,true);
  const behind=down(.5,-13.5);assert.equal(behind.kind,'ground');
  assert.equal(world.place(behind,'dirt',distantPlayer).ok,true);
  assert.equal(world.remove(down(.5,-13.5)).ok,true);
  assert.equal(world.remove(down(4.5,2.5)).ok,true);
  assert.equal(world.remove(down(4.5,2.5)).ok,false);
  assert.equal(world.count,0);
});
check('curved crater ray uses actual terrain height and a slope normal; blocks are buried flush',()=>{
  const crater=sandbox.createAsciiBuildWorld({terrain:(x,z)=>-.9*Math.exp(-((x-4.5)**2+(z-5.5)**2)),sphere});
  const hit=crater.raycast([4.75,2,5.5],[0,-1,0]);
  const expected=-.9*Math.exp(-(.25**2));assert.ok(Math.abs(hit.point[1]-expected)<.001);
  assert.ok(hit.normal[0]<-.1&&hit.normal[1]>.5);
  assert.equal(crater.place(hit,'stone',distantPlayer).ok,true);
  const cube=crater.raycast([4.75,2,5.5],[0,-1,0]);
  assert.deepEqual(values(cube.cell),[4,-1,5]);assert.ok(Math.abs(cube.point[1])<.001);
  assert.equal(crater.floor(4.75,5.5,expected),0);
});
check('tree trunk silhouette and solid/ray bounds match, while seeds leave the entrance lane open',()=>{
  const hash=n=>{const v=Math.sin(n*127.1+311.7)*43758.5453;return v-Math.floor(v);};
  const x=-25+(hash(1)-.5)*.9,z=-4+(hash(43)-.5)*1.5;
  const nearby=world.points(x,z),bark=nearby.filter(p=>p[7]==='bark'&&Math.abs(p[0]-x)<.5&&Math.abs(p[2]-z)<.5);
  assert.ok(bark.length>100);const maxX=Math.max(...bark.map(p=>p[0]));
  const hit=world.raycast([x+2,1,z],[-1,0,0]);assert.equal(hit.kind,'tree');assert.equal(hit.material,'wood');
  assert.ok(Math.abs(hit.point[0]-maxX)<1e-6);assert.equal(hit.removable,false);
  assert.equal(world.blocked(x,z,0),true);assert.equal(world.blocked(maxX+.30,z,0),false);
  assert.ok(nearby.some(p=>p[7]==='leaf'));
  assert.equal(world.remove(hit).ok,false);assert.equal(world.place(hit,'wood',distantPlayer).ok,false);
  assert.equal(world.blocked(0,-2,0),false);
});
check('static nearby points are cached and tree generation does not repeat terrain sampling',()=>{
  const a=world.points(0,0),samples=terrainSamples,b=world.points(1,1);
  assert.equal(a,b);assert.equal(terrainSamples,samples);
  assert.ok(a.length>0&&a.length<40000);
  const far=world.points(500,500);assert.equal(far.length,0);
});
check('512 user blocks is a hard cap, delete frees capacity, and reset clears all edits',()=>{
  world.reset();let placed=0;
  for(let z=1;z<80&&placed<512;z++)for(let x=31;x<80&&placed<512;x++){
    const hit=down(x+.5,z+.5);if(hit?.kind!=='ground')continue;
    if(world.place(hit,'stone',distantPlayer).ok)placed++;
  }
  assert.equal(placed,512);assert.equal(world.count,512);
  assert.equal(world.place(down(79.5,79.5),'dirt',distantPlayer).ok,false);
  assert.equal(world.remove(down(31.5,1.5)).ok,true);assert.equal(world.count,511);
  assert.equal(world.place(down(79.5,79.5),'dirt',distantPlayer).ok,true);assert.equal(world.count,512);
  const editedPoints=world.points(40,8);assert.ok(editedPoints.length<90000);
  world.reset();assert.equal(world.count,0);assert.equal(world.floor(31.5,1.5,0),0);
  assert.equal(down(31.5,1.5).kind,'ground');
});
console.log(JSON.stringify({checks,nearbyPoints:world.points(0,0).length}));

const ads=sandbox.createShelterAds();
const glyphs=ads.filter(p=>p[8]).map(p=>p[8]).join('');
assert.ok(glyphs.includes('MSG')&&glyphs.includes('POSTS.TOPICS')&&glyphs.includes('AGENTCOLLAB')&&glyphs.includes('msg.lmm.best'));
assert.ok(glyphs.includes('API.LMM.BEST')&&glyphs.includes('AIAPIRELAY')&&glyphs.includes('api.lmm.best'));
assert.ok(ads.some(p=>p[7]==='adcyan')&&ads.some(p=>p[7]==='adamber'));
assert.ok(ads.every(p=>p[2]>=-9.6&&p[2]<=-9.5));
console.log('PASS physical colored MSG and API relay posters');

"""
    result = subprocess.run(
        [shutil.which('node'), '-e', script],
        input=functions, text=True, capture_output=True, check=False, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
